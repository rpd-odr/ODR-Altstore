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
    """Adapter for the public decrypt.day app pages.

    decrypt.day exposes app pages such as /app/id<trackId>; the actual
    download URL contains a short-lived token and therefore must be
    discovered from the page instead of being hard-coded.
    """

    APP_URL_TEMPLATE = "https://decrypt.day/app/id{app_store_id}"

    def __init__(self, max_retries: int = 3):
        super().__init__(max_retries=max_retries)
        self.resolved_version: Optional[str] = None
        self.resolved_build: str = ""

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

    def _page_url(self, app_store_id: str, metadata_url: Optional[str]) -> str:
        if metadata_url:
            return metadata_url
        return self.APP_URL_TEMPLATE.format(app_store_id=app_store_id)

    def get_latest_metadata(self, bundle_id: str, metadata_url: Optional[str] = None):
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

        page_url = self._page_url(app_store_id, metadata_url)
        headers = {
            "User-Agent": os.getenv(
                "IPA_PROVIDER_USER_AGENT",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/131 Safari/537.36 ODR-Alt/1.0",
            ),
            "Accept": "text/html,application/xhtml+xml",
        }
        timeout = float(os.getenv("IPA_PROVIDER_TIMEOUT", "30"))

        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
            response = self._request_with_retry(client, page_url, headers=headers)
            try:
                body = response.text
            finally:
                response.close()

        download_url = self._extract_download_url(page_url, body)
        if not download_url:
            raise ProviderError(
                f"Не удалось найти ссылку на IPA на decrypt.day ({sanitize_url(page_url)}). "
                "Возможна Cloudflare-проверка или изменился HTML страницы."
            )

        # The page is the authoritative source for the current available IPA,
        # but its HTML may not expose a machine-readable version/build. The
        # IPA validator later obtains the exact values from Payload/*.app/Info.plist.
        self.resolved_version = "latest"
        self.resolved_build = ""
        return {
            "bundle_id": bundle_id,
            "version": "latest",
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
        meta = self.get_latest_metadata(bundle_id, metadata_url=metadata_url)
        return meta["download_url"]
