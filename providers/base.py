import hashlib
import logging
import os
import plistlib
import re
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlparse, urlunparse

import httpx

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - optional until requirements are installed
    curl_requests = None

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
        self.user_agent = os.getenv("IPA_PROVIDER_USER_AGENT", user_agent or "ODR-Alt-Repository-Provider/2.3")

    def log_gh_annotation(self, level: str, message: str) -> None:
        if os.getenv("GITHUB_ACTIONS") == "true":
            sanitized = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::{level}::{sanitized}", file=sys.stderr)

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
        parsed = urlparse(url)
        is_decrypt_day = parsed.hostname == "decrypt.day"
        if is_decrypt_day:
            app_match = re.search(r"/app/id\d+", parsed.path)
            if app_match:
                app_url = f"https://decrypt.day{app_match.group(0)}"
                headers.setdefault("Referer", app_url)
            headers.setdefault("Accept", "application/octet-stream,application/zip,*/*")
            # curl_cffi gives decrypt.day a real browser TLS/HTTP fingerprint.
            # A plain httpx request was consistently answered with HTTP 403.
            headers.setdefault("Sec-Fetch-Dest", "document")
            headers.setdefault("Sec-Fetch-Mode", "navigate")
            headers.setdefault("Sec-Fetch-Site", "same-origin")
            headers.setdefault("Accept-Language", "en-US,en;q=0.9")
        token = os.getenv("IPA_PROVIDER_TOKEN")
        if token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {token}"
        clean_url = sanitize_url(url)
        hasher = hashlib.sha256()
        downloaded = 0
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ipa") as tmp:
                tmp_path = tmp.name

            if is_decrypt_day and curl_requests is not None:
                client = curl_requests.Session(impersonate=os.getenv("IPA_CURL_IMPERSONATE", "chrome"))
                if cookies:
                    client.cookies.update(cookies)
                client.headers.update(headers)
                request_kwargs = {"timeout": self.timeout, "allow_redirects": True, "stream": True}
            else:
                client = httpx.Client(
                    timeout=httpx.Timeout(self.timeout),
                    headers=headers,
                    cookies=cookies or None,
                    follow_redirects=True,
                )
                request_kwargs = {}

            try:
                response = None
                for attempt in range(1, self.max_retries + 1):
                    try:
                        if is_decrypt_day and curl_requests is not None:
                            response = client.get(url, **request_kwargs)
                        else:
                            response = client.send(client.build_request("GET", url), stream=True)
                        status = response.status_code
                        if status == 404:
                            response.close()
                            raise ProviderError(f"HTTP 404: ресурс не найден по адресу {clean_url}")
                        if status == 429 or status == 403 or status >= 500:
                            if attempt < self.max_retries:
                                retry_after = response.headers.get("Retry-After")
                                try:
                                    delay = max(0.0, float(retry_after)) if retry_after else float(2 ** attempt)
                                except ValueError:
                                    delay = float(2 ** attempt)
                                response.close()
                                logger.warning(
                                    "HTTP %s на попытке %s/%s, повтор через %ss",
                                    status, attempt, self.max_retries, delay,
                                )
                                time.sleep(delay)
                                continue
                            response.close()
                            raise ProviderError(f"HTTP {status} после {self.max_retries} попыток ({clean_url})")
                        response.raise_for_status()
                        break
                    except (httpx.HTTPError, Exception) as exc:
                        # curl_cffi and httpx expose different exception classes; only
                        # retry transport/HTTP failures here, while preserving ProviderError.
                        if isinstance(exc, ProviderError):
                            raise
                        if response is not None:
                            response.close()
                        if attempt >= self.max_retries:
                            raise ProviderError(f"Сетевая ошибка при скачивании IPA ({clean_url}): {exc}") from exc
                        time.sleep(2 ** attempt)

                if response is None:
                    raise ProviderError(f"Не удалось получить ответ от {clean_url}")
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > MAX_IPA_SIZE:
                    response.close()
                    raise ProviderError(f"Размер IPA превышает лимит {MAX_IPA_SIZE} байт")
                final_url = sanitize_url(str(response.url))
                content_type = response.headers.get("Content-Type", "")
                try:
                    with open(tmp_path, "wb") as out:
                        iterator = response.iter_content(chunk_size=65536) if is_decrypt_day and curl_requests is not None else response.iter_bytes(chunk_size=65536)
                        for chunk in iterator:
                            if not chunk:
                                continue
                            downloaded += len(chunk)
                            if downloaded > MAX_IPA_SIZE:
                                raise ProviderError(f"Размер IPA превысил лимит {MAX_IPA_SIZE} байт")
                            hasher.update(chunk)
                            out.write(chunk)
                finally:
                    response.close()
            finally:
                client.close()

            if not zipfile.is_zipfile(tmp_path):
                with open(tmp_path, "rb") as probe:
                    head = probe.read(64)
                head_hex = head.hex(" ")
                head_text = head.decode("utf-8", errors="replace").replace("\r", "\\r").replace("\n", "\\n")
                logger.error(
                    "IPA download is not a ZIP: status=%s content_type=%r downloaded=%d final_url=%s head_hex=%s head_text=%r sha256=%s",
                    status, content_type, downloaded, final_url, head_hex, head_text[:200], hasher.hexdigest(),
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
            return {"bundle_id": bundle_id, "version": version, "build": build, "size": downloaded, "sha256": hasher.hexdigest()}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def get_latest(self, bundle_id: str, version: str, dry_run: bool = False) -> IPAMetadata:
        raise NotImplementedError
