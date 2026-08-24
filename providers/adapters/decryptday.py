import html
import logging
import os
import re
from typing import Optional
from urllib.parse import urljoin

import httpx

from providers.adapters.base import BaseDecryptAdapter
from providers.base import ProviderError, sanitize_url
from providers.registry import AdapterRegistry

logger = logging.getLogger("DecryptDayAdapter")


@AdapterRegistry.register("decryptday")
class DecryptDayAdapter(BaseDecryptAdapter):
    """Adapter for decrypt.day's machine-readable Svelte data endpoints."""

    APP_URL_TEMPLATE = "https://decrypt.day/app/id{app_store_id}"
    DATA_URL_TEMPLATE = "https://decrypt.day/app/id{app_store_id}/__data.json"

    def __init__(self, max_retries: int = 3):
        super().__init__(max_retries=max_retries)
        self.resolved_version: Optional[str] = None
        self.resolved_build: str = ""
        self._resolved_ipa_url: Optional[str] = None

    @staticmethod
    def _extract_download_url(page_url: str, body: str) -> Optional[str]:
        text = html.unescape(body)
        patterns = (
            r"https?://[^\"'<>\s]+/app/id\d+/dl/[^\"'<>\s]+",
            r"(?P<path>/app/id\d+/dl/[^\"'<>\s]+)",
            r"(?P<ipa>https?://[^\"'<>\s]+\.ipa(?:\?[^\"'<>\s]*)?)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(0)
                if value.startswith("/"):
                    value = urljoin(page_url, value)
                return value.rstrip("\\),;])")
        return None

    @staticmethod
    def _decode_svelte(values):
        """Resolve SvelteKit's integer references in a serialized data array."""
        if not isinstance(values, list) or not values:
            return None

        cache = {}
        active = set()

        def resolve(index):
            if not isinstance(index, int) or index < 0 or index >= len(values):
                return None
            if index in cache:
                return cache[index]
            if index in active:
                return None
            active.add(index)
            result = expand(values[index])
            active.remove(index)
            cache[index] = result
            return result

        def expand(value):
            if isinstance(value, int):
                return resolve(value)
            if isinstance(value, list):
                return [expand(item) for item in value]
            if isinstance(value, dict):
                return {key: expand(item) for key, item in value.items()}
            return value

        return resolve(0)

    @staticmethod
    def _get_string(value, key: str) -> Optional[str]:
        item = value.get(key) if isinstance(value, dict) else None
        return item if isinstance(item, str) else None

    @staticmethod
    def _get_bool(value, key: str) -> bool:
        item = value.get(key) if isinstance(value, dict) else None
        return item is True

    @staticmethod
    def _file_payload(app_id: str, version: str) -> str:
        # SvelteKit form action payload used by decrypt.day's /files endpoint.
        values = ["appId", app_id, "version", version, "isPremier"]
        data = [0xA3]
        for value in values:
            encoded = value.encode("utf-8")
            if len(encoded) <= 15:
                data.append(0x60 + len(encoded))
            else:
                data.extend((0x78, len(encoded)))
            data.extend(encoded)
        data.append(0xF7)
        return ",".join(str(item) for item in data)

    def _page_url(self, app_store_id: str, metadata_url: Optional[str]) -> str:
        if metadata_url:
            return metadata_url
        return self.APP_URL_TEMPLATE.format(app_store_id=app_store_id)

    def _app_store_id(self, metadata_url: Optional[str]) -> str:
        app_store_id = os.getenv("IPA_DECRYPT_DAY_APP_ID")
        if not app_store_id and metadata_url:
            match = re.search(r"/app/id(\d+)", metadata_url)
            if match:
                app_store_id = match.group(1)
        if not app_store_id:
            raise ProviderError(
                "Decrypt.day requires App Store ID: set IPA_DECRYPT_DAY_APP_ID "
                "or metadataUrl to https://decrypt.day/app/id<trackId>"
            )
        return app_store_id

    def _request_headers(self, page_url: str) -> dict:
        return {
            "User-Agent": os.getenv(
                "IPA_PROVIDER_USER_AGENT",
                "PlayCover/3.0 CFNetwork/1494.0.7 Darwin/23.4.0",
            ),
            "Accept": "application/json,text/html,application/xhtml+xml",
            "Accept-Language": os.getenv("IPA_PROVIDER_ACCEPT_LANGUAGE", "en-US,en;q=0.9"),
            "Referer": page_url,
        }

    def _get_detail(self, client: httpx.Client, app_store_id: str, page_url: str):
        data_url = self.DATA_URL_TEMPLATE.format(app_store_id=app_store_id)
        response = self._request_with_retry(
            client, data_url, headers=self._request_headers(page_url)
        )
        try:
            document = response.json()
        except ValueError as exc:
            raise ProviderError(f"decrypt.day returned invalid JSON ({sanitize_url(data_url)})") from exc
        finally:
            response.close()

        for node in document.get("nodes", []) if isinstance(document, dict) else []:
            values = node.get("data") if isinstance(node, dict) else None
            root = self._decode_svelte(values)
            app = root.get("app") if isinstance(root, dict) else None
            bundle = self._get_string(app, "bundle_id")
            if not bundle:
                continue

            versions = []
            for item in (root.get("versions", []) if isinstance(root, dict) else []):
                name = self._get_string(item, "name")
                if name:
                    versions.append(name)

            app_id = self._get_string(app, "id")
            if not app_id:
                raise ProviderError("decrypt.day metadata omitted its internal app ID")
            return app_id, bundle, versions

        raise ProviderError("decrypt.day did not return recognizable app metadata")

    def _get_file_id(self, client: httpx.Client, app_store_id: str, decrypt_day_id: str, version: str, page_url: str):
        boundary = "----WebKitFormBoundary" + os.urandom(16).hex()
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="data"\r\n\r\n'
            f"{self._file_payload(decrypt_day_id, version)}\r\n"
            f"--{boundary}--\r\n"
        )
        headers = self._request_headers(page_url)
        headers.update({
            "Origin": "https://decrypt.day",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
        files_url = f"https://decrypt.day/app/id{app_store_id}?/files"
        response = self._request_with_retry(
            client, files_url, method="POST", headers=headers, content=body.encode("utf-8")
        )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise ProviderError(f"decrypt.day file lookup returned invalid JSON ({sanitize_url(files_url)})") from exc
        finally:
            response.close()

        serialized = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(serialized, str):
            raise ProviderError("decrypt.day returned an unrecognized file list")
        try:
            values = __import__("json").loads(serialized)
        except ValueError as exc:
            raise ProviderError("decrypt.day returned malformed serialized file data") from exc
        root = self._decode_svelte(values)
        files = root.get("data", {}).get("files", []) if isinstance(root, dict) else []

        for file in files:
            if not isinstance(file, dict):
                continue
            if self._get_bool(file, "premium") or self._get_bool(file, "login_required"):
                continue
            file_id = self._get_string(file, "id")
            if file_id:
                return file_id
        return None

    def get_latest_metadata(self, bundle_id: str, metadata_url: Optional[str] = None):
        app_store_id = self._app_store_id(metadata_url)
        page_url = self._page_url(app_store_id, metadata_url)
        timeout = float(os.getenv("IPA_PROVIDER_TIMEOUT", "30"))

        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
            decrypt_day_id, remote_bundle, versions = self._get_detail(client, app_store_id, page_url)
            if remote_bundle != bundle_id:
                raise ProviderError(
                    f"decrypt.day bundle mismatch: expected {bundle_id}, got {remote_bundle}"
                )

            if not versions:
                raise ProviderError("decrypt.day returned no available app versions")

            version = versions[-1]
            file_id = self._get_file_id(client, app_store_id, decrypt_day_id, version, page_url)
            if not file_id:
                raise ProviderError(f"decrypt.day has no free/login-free IPA for version {version}")

        download_url = f"https://decrypt.day/app/id{app_store_id}/dl/{file_id}"
        self.resolved_version = version
        self.resolved_build = ""
        self._resolved_ipa_url = download_url
        return {
            "bundle_id": bundle_id,
            "version": version,
            "build": "",
            "download_url": download_url,
        }

    def resolve_ipa_url(
        self,
        bundle_id: str,
        version: str,
        timeout: float = 30.0,
        metadata_url: Optional[str] = None,
    ) -> str:
        if self._resolved_ipa_url and version == self.resolved_version:
            return self._resolved_ipa_url
        meta = self.get_latest_metadata(bundle_id, metadata_url=metadata_url)
        if version not in ("latest", "newest", meta["version"]):
            raise ProviderError(
                f"decrypt.day returned latest version {meta['version']}, requested {version}"
            )
        return meta["download_url"]
