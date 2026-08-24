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
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout),
                headers=headers,
                cookies=cookies or None,
                follow_redirects=True,
            ) as client:
                response = None
                for attempt in range(1, self.max_retries + 1):
                    try:
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
                                logger.warning(f"HTTP {status} на попытке {attempt}/{self.max_retries}, повтор через {delay}s")
                                time.sleep(delay)
                                continue
                            response.close()
                            raise ProviderError(f"HTTP {status} после {self.max_retries} попыток ({clean_url})")
                        response.raise_for_status()
                        break
                    except httpx.HTTPError as exc:
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
                try:
                    with open(tmp_path, "wb") as out:
                        for chunk in response.iter_bytes(chunk_size=65536):
                            downloaded += len(chunk)
                            if downloaded > MAX_IPA_SIZE:
                                raise ProviderError(f"Размер IPA превысил лимит {MAX_IPA_SIZE} байт")
                            hasher.update(chunk)
                            out.write(chunk)
                finally:
                    response.close()
            if not zipfile.is_zipfile(tmp_path):
                raise ProviderError("Скачанный файл не является валидным ZIP/IPA архивом")
            with zipfile.ZipFile(tmp_path, "r") as archive:
                if archive.testzip() is not None:
                    raise ProviderError("IPA содержит повреждённый ZIP-файл")
                plist_name = next((n for n in archive.namelist() if n.startswith("Payload/") and n.endswith(".app/Info.plist") and n.count("/") == 2), None)
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
