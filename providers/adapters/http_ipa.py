import logging
import os
import time
from typing import Any, Dict, Optional

import httpx
from packaging.version import InvalidVersion, Version

from providers.adapters.base import BaseDecryptAdapter
from providers.base import ProviderError, sanitize_url
from providers.registry import AdapterRegistry

logger = logging.getLogger("HttpIPASourceAdapter")


@AdapterRegistry.register("http_ipa")
class HttpIPASourceAdapter(BaseDecryptAdapter):
    """Generic adapter for an explicitly authorized HTTP IPA metadata source."""

    def __init__(self, max_retries: int = 3):
        super().__init__(max_retries=max_retries)
        self.resolved_version: Optional[str] = None
        self.resolved_build: str = ""

    @staticmethod
    def _version_key(value: str):
        try:
            return (1, Version(value))
        except InvalidVersion:
            # Keep unusual iOS version strings sortable without silently
            # treating them as errors.
            parts = []
            for token in value.replace("-", ".").split("."):
                try:
                    parts.append((1, int(token)))
                except ValueError:
                    parts.append((0, token.lower()))
            return (0, tuple(parts))

    @staticmethod
    def _retry_after(response: httpx.Response) -> Optional[float]:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return min(max(float(value), 0.0), 60.0)
        except ValueError:
            return None

    def _request_with_retry(self, client: httpx.Client, url: str, **kwargs) -> httpx.Response:
        clean = sanitize_url(url)
        for attempt in range(1, self.max_retries + 1):
            try:
                response = client.get(url, **kwargs)
                if response.status_code in (429, 500, 502, 503, 504):
                    if attempt < self.max_retries:
                        delay = self._retry_after(response) or (2 ** attempt)
                        logger.warning(
                            "HTTP %s from %s; retry %s/%s in %ss",
                            response.status_code, clean, attempt, self.max_retries, delay,
                        )
                        response.close()
                        time.sleep(delay)
                        continue
                    raise ProviderError(
                        f"HTTP {response.status_code} after {self.max_retries} attempts ({clean})"
                    )
                if response.status_code == 404:
                    raise ProviderError(f"Resource not found (HTTP 404): {clean}")
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                if attempt == self.max_retries:
                    raise ProviderError(f"Network error for {clean}: {exc}") from exc
                time.sleep(2 ** attempt)
        raise ProviderError(f"Unable to request {clean}")

    def get_latest_metadata(self, bundle_id: str, metadata_url: Optional[str] = None) -> Dict[str, Any]:
        url = metadata_url or os.getenv("IPA_SOURCE_METADATA_URL")
        if not url:
            raise ProviderError("IPA_SOURCE_METADATA_URL or metadataUrl is required")

        token = os.getenv("IPA_PROVIDER_TOKEN")
        headers = {"User-Agent": os.getenv("IPA_PROVIDER_USER_AGENT", "ODR-Alt/1.0")}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        timeout = float(os.getenv("IPA_PROVIDER_TIMEOUT", "30"))
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
            response = self._request_with_retry(client, url, headers=headers)
            try:
                payload = response.json()
            except (ValueError, TypeError) as exc:
                raise ProviderError(f"Invalid JSON from {sanitize_url(url)}: {exc}") from exc
            finally:
                response.close()

        raw_items = payload if isinstance(payload, list) else payload.get("apps", payload.get("sources", [payload])) if isinstance(payload, dict) else []
        matches = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            item_bundle = item.get("bundleIdentifier") or item.get("bundle_id")
            if item_bundle != bundle_id:
                continue
            version = item.get("version") or item.get("CFBundleShortVersionString")
            download_url = item.get("downloadUrl") or item.get("download_url") or item.get("ipa_url")
            if version and download_url:
                matches.append({
                    "bundle_id": bundle_id,
                    "version": str(version),
                    "build": str(item.get("build") or item.get("CFBundleVersion") or ""),
                    "download_url": str(download_url),
                })

        if not matches:
            raise ProviderError(f"No metadata for Bundle ID '{bundle_id}'")

        selected = max(matches, key=lambda item: self._version_key(item["version"]))
        self.resolved_version = selected["version"]
        self.resolved_build = selected["build"]
        return selected

    def resolve_ipa_url(
        self,
        bundle_id: str,
        version: str,
        timeout: float = 30.0,
        metadata_url: Optional[str] = None,
    ) -> str:
        meta = self.get_latest_metadata(bundle_id, metadata_url=metadata_url)
        if version not in ("latest", "newest") and self._version_key(meta["version"]) != self._version_key(version):
            raise ProviderError(
                f"Requested version {version} is not the latest source version {meta['version']}"
            )
        return meta["download_url"]
