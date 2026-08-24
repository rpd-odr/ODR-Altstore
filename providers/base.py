import hashlib
import logging
import os
import plistlib
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

import httpx

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional dependency
    sync_playwright = None

logger = logging.getLogger("IPASourceProvider")
MAX_IPA_SIZE = 2 * 1024 * 1024 * 1024


def sanitize_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        return urlunparse(parsed._replace(netloc=host, query="", fragment=""))
    except Exception:
        return "[SANITIZED_URL]"


@dataclass
class IPAMetadata:
    bundle_id: str
    version: str
    build: str
    ipa_url: str
    source: str
    size: int
    sha256: str
    verified: bool


class ProviderError(Exception):
    pass


class IPASourceProvider:
    def __init__(self, timeout: float = 30.0, max_retries: int = 3, user_agent: Optional[str] = None):
        self.timeout = float(os.getenv("IPA_PROVIDER_TIMEOUT", timeout))
        self.max_retries = max(1, max_retries)
        self.user_agent = os.getenv("IPA_PROVIDER_USER_AGENT", user_agent or "ODR-Alt-Repository-Provider/2.5")

    def log_gh_annotation(self, level: str, message: str) -> None:
        if os.getenv("GITHUB_ACTIONS") == "true":
            sanitized = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::{level}::{sanitized}", file=sys.stderr)

    def _download_with_browser(self, url: str, tmp_path: str) -> Dict[str, Any]:
        if sync_playwright is None:
            raise ProviderError("Для decrypt.day нужен Playwright: установите playwright и Chromium")

        logger.info("decrypt.day: открываем IPA URL в Chromium для прохождения web challenge")
        proxy_url = os.getenv("IPA_PROVIDER_PROXY") or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
        launch_kwargs: Dict[str, Any] = {
            "headless": True,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if proxy_url:
            launch_kwargs["proxy"] = {"server": proxy_url}

        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                user_agent=os.getenv(
                    "IPA_PROVIDER_USER_AGENT",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                ),
                locale="en-US",
                accept_downloads=True,
            )
            page = context.new_page()
            try:
                try:
                    with page.expect_download(timeout=7000) as download_info:
                        page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                    download = download_info.value
                    download.save_as(tmp_path)
                    return {"content_type": "application/octet-stream", "final_url": sanitize_url(url)}
                except Exception as first_exc:
                    logger.info("decrypt.day direct browser download not immediate: %s", first_exc)

                # If Cloudflare presented a challenge, the browser can execute its
                # JavaScript and establish the clearance cookie. Reuse that same
                # browser context for the actual file request instead of copying
                # cookies into a second client too early.
                page.wait_for_timeout(5000)
                cookies = context.cookies(url)
                logger.info("decrypt.day browser session established (%d cookies)", len(cookies))

                response = context.request.get(
                    url,
                    timeout=int(self.timeout * 1000),
                    fail_on_status_code=False,
                    headers={"Accept": "application/octet-stream,application/zip,*/*"},
                )
                if response.status != 200:
                    raise ProviderError(
                        f"decrypt.day browser session вернула HTTP {response.status} для {sanitize_url(url)}"
                    )
                body = response.body()
                if len(body) > MAX_IPA_SIZE:
                    raise ProviderError(f"Размер IPA превышает лимит {MAX_IPA_SIZE} байт")
                with open(tmp_path, "wb") as out:
                    out.write(body)
                return {
                    "content_type": response.headers.get("content-type", ""),
                    "final_url": sanitize_url(response.url),
                }
            finally:
                context.close()
                browser.close()

    def download_and_inspect_ipa(
        self,
        url: str,
        expected_bundle_id: str,
        expected_version: Optional[str] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        cookies: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        headers = {"User-Agent": self.user_agent}
        if extra_headers:
            headers.update({k: v for k, v in extra_headers.items() if v is not None})
        token = os.getenv("IPA_PROVIDER_TOKEN")
        if token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {token}"

        parsed = urlparse(url)
        is_decrypt_day = parsed.hostname == "decrypt.day"
        clean_url = sanitize_url(url)
        hasher = hashlib.sha256()
        downloaded = 0
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ipa") as tmp:
                tmp_path = tmp.name

            status = 0
            content_type = ""
            final_url = clean_url

            if is_decrypt_day:
                browser_error = None
                try:
                    browser_meta = self._download_with_browser(url, tmp_path)
                    content_type = browser_meta["content_type"]
                    final_url = browser_meta["final_url"]
                    downloaded = os.path.getsize(tmp_path)
                    with open(tmp_path, "rb") as source:
                        for chunk in iter(lambda: source.read(65536), b""):
                            hasher.update(chunk)
                    status = 200
                except ProviderError:
                    raise
                except Exception as exc:
                    browser_error = exc

                if browser_error is not None:
                    logger.warning("decrypt.day browser download failed: %s; falling back to HTTP", browser_error)
                    with httpx.Client(
                        timeout=httpx.Timeout(self.timeout),
                        headers=headers,
                        cookies=cookies or None,
                        follow_redirects=True,
                    ) as client:
                        response = None
                        for attempt in range(1, self.max_retries + 1):
                            response = client.get(url)
                            status = response.status_code
                            content_type = response.headers.get("Content-Type", "")
                            if status == 200:
                                break
                            if status == 404:
                                raise ProviderError(f"HTTP 404: ресурс не найден по адресу {clean_url}")
                            if attempt >= self.max_retries:
                                raise ProviderError(f"HTTP {status} после {self.max_retries} попыток ({clean_url})")
                            time.sleep(2 ** attempt)
                        final_url = sanitize_url(str(response.url))
                        with open(tmp_path, "wb") as out:
                            for chunk in response.iter_bytes(chunk_size=65536):
                                if not chunk:
                                    continue
                                downloaded += len(chunk)
                                if downloaded > MAX_IPA_SIZE:
                                    raise ProviderError(f"Размер IPA превысил лимит {MAX_IPA_SIZE} байт")
                                hasher.update(chunk)
                                out.write(chunk)
            else:
                with httpx.Client(
                    timeout=httpx.Timeout(self.timeout),
                    headers=headers,
                    cookies=cookies or None,
                    follow_redirects=True,
                ) as client:
                    response = None
                    for attempt in range(1, self.max_retries + 1):
                        try:
                            response = client.get(url)
                            status = response.status_code
                            if status == 404:
                                raise ProviderError(f"HTTP 404: ресурс не найден по адресу {clean_url}")
                            if status == 429 or status >= 500:
                                if attempt < self.max_retries:
                                    time.sleep(2 ** attempt)
                                    continue
                            response.raise_for_status()
                            break
                        except ProviderError:
                            raise
                        except httpx.HTTPError as exc:
                            if attempt >= self.max_retries:
                                raise ProviderError(f"Сетевая ошибка при скачивании IPA ({clean_url}): {exc}") from exc
                            time.sleep(2 ** attempt)
                    if response is None:
                        raise ProviderError(f"Не удалось получить ответ от {clean_url}")
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > MAX_IPA_SIZE:
                        raise ProviderError(f"Размер IPA превышает лимит {MAX_IPA_SIZE} байт")
                    status = response.status_code
                    content_type = response.headers.get("Content-Type", "")
                    final_url = sanitize_url(str(response.url))
                    with open(tmp_path, "wb") as out:
                        for chunk in response.iter_bytes(chunk_size=65536):
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > MAX_IPA_SIZE:
                                raise ProviderError(f"Размер IPA превысил лимит {MAX_IPA_SIZE} байт")
                            hasher.update(chunk)
                            out.write(chunk)

            if not zipfile.is_zipfile(tmp_path):
                with open(tmp_path, "rb") as probe:
                    head = probe.read(128)
                logger.error(
                    "IPA download is not a ZIP: status=%s content_type=%r downloaded=%d final_url=%s head_hex=%s head_text=%r sha256=%s",
                    status, content_type, downloaded, final_url, head.hex(" "),
                    head.decode("utf-8", errors="replace")[:200].replace("\r", "\\r").replace("\n", "\\n"),
                    hasher.hexdigest(),
                )
                raise ProviderError("Скачанный файл не является валидным ZIP/IPA архивом")

            with zipfile.ZipFile(tmp_path, "r") as archive:
                if archive.testzip() is not None:
                    raise ProviderError("IPA содержит повреждённый ZIP-файл")
                plist_name = next(
                    (n for n in archive.namelist() if n.startswith("Payload/") and n.endswith(".app/Info.plist") and n.count("/") == 2),
                    None,
                )
                if not plist_name:
                    raise ProviderError("IPA не содержит Payload/*.app/Info.plist")
                plist = plistlib.loads(archive.read(plist_name))

            bundle_id = plist.get("CFBundleIdentifier")
            version = plist.get("CFBundleShortVersionString")
            build = str(plist.get("CFBundleVersion", ""))
            if bundle_id != expected_bundle_id:
                raise ProviderError(f"Несовпадение Bundle ID: ожидался '{expected_bundle_id}', получен '{bundle_id}'")
            if expected_version not in (None, "", "latest", "newest") and version != expected_version:
                raise ProviderError(f"Несовпадение версии: ожидалась '{expected_version}', получена '{version}'")
            return {
                "bundle_id": bundle_id,
                "version": version,
                "build": build,
                "size": downloaded,
                "sha256": hasher.hexdigest(),
            }
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def get_latest(self, bundle_id: str, version: str, dry_run: bool = False) -> IPAMetadata:
        raise NotImplementedError
