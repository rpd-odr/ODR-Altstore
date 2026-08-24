import logging
import os
from typing import Any, Optional

import httpx

from providers.adapters.base import BaseDecryptAdapter
from providers.base import ProviderError, sanitize_url
from providers.registry import AdapterRegistry

logger = logging.getLogger("DecryptIPAAdapter")


@AdapterRegistry.register("decryptipa")
class DecryptIPAAdapter(BaseDecryptAdapter):
    """Adapter for the public Decrypt.day library feed.

    Decrypt.day exposes its library as a JSON feed used by IPA-library clients.
    We only consume the published feed; authentication or scraping of user
    accounts is deliberately not part of this adapter.

    ``IPA_PROVIDER_URL`` remains supported for backwards compatibility with
    the original generic HTTP resolver used by the provider tests and older
    deployments. An explicit ``metadata_url`` uses the generic HTTP adapter.
    """

    DEFAULT_LIBRARY_URL = "https://decrypt.day/library/data.json"

    def __init__(self, max_retries: int = 3):
        super().__init__(max_retries=max_retries)
        self.library_url = os.getenv("DECRYPT_DAY_LIBRARY_URL", self.DEFAULT_LIBRARY_URL)
        self.resolved_version: Optional[str] = None

    @staticmethod
    def _version_key(version: str) -> tuple:
        """Best-effort comparison for dotted App Store versions."""
        parts = []
        for part in str(version).split("."):
            digits = "".join(ch for ch in part if ch.isdigit())
            parts.append(int(digits or 0))
        return tuple(parts)

    @staticmethod
    def _app_matches(item: dict[str, Any], bundle_id: str) -> bool:
        candidates = (
            item.get("id"),
            item.get("bundleIdentifier"),
            item.get("bundle_id"),
            item.get("bundleName"),
            item.get("bundleId"),
        )
        return bundle_id in {str(value) for value in candidates if value}

    def _fetch_library(self, timeout: float) -> list[dict[str, Any]]:
        headers = {
            "User-Agent": os.getenv(
                "IPA_PROVIDER_USER_AGENT", "ODR-Alt-Repository-Provider/2.4"
            ),
            "Accept": "application/json",
        }
        clean_url = sanitize_url(self.library_url)

        with httpx.Client(timeout=httpx.Timeout(timeout), headers=headers, follow_redirects=True) as client:
            response = None
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = client.get(self.library_url)
                    if response.status_code == 404:
                        raise ProviderError(f"Decrypt.day library не найдена: {clean_url}")
                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < self.max_retries:
                            delay = self._retry_delay(response, attempt)
                            logger.warning(
                                "Decrypt.day HTTP %s, retry %s/%s через %.1fs",
                                response.status_code, attempt, self.max_retries, delay,
                            )
                            import time
                            time.sleep(delay)
                            continue
                        raise ProviderError(
                            f"Decrypt.day вернул HTTP {response.status_code} после "
                            f"{self.max_retries} попыток ({clean_url})"
                        )
                    response.raise_for_status()
                    break
                except httpx.RequestError as exc:
                    if attempt >= self.max_retries:
                        raise ProviderError(f"Ошибка сети Decrypt.day ({clean_url}): {exc}") from exc
                    import time
                    delay = float(2 ** attempt)
                    time.sleep(delay)

            if response is None:
                raise ProviderError(f"Не удалось получить ответ Decrypt.day ({clean_url})")

            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError("Decrypt.day library вернула некорректный JSON") from exc

        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("apps", "items", "data", "library"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        raise ProviderError("Неизвестный формат Decrypt.day library JSON")

    def resolve_ipa_url(
        self,
        bundle_id: str,
        version: str,
        timeout: float = 30.0,
        metadata_url: Optional[str] = None,
    ) -> str:
        # Explicit metadataUrl is handled by the generic HTTP metadata adapter.
        if metadata_url:
            from providers.adapters.http_ipa import HttpIPASourceAdapter

            adapter = HttpIPASourceAdapter(max_retries=self.max_retries)
            return adapter.resolve_ipa_url(
                bundle_id,
                version,
                timeout=timeout,
                metadata_url=metadata_url,
            )

        # Preserve compatibility with the original resolver API. This is also
        # useful for the existing test suite and older source configurations.
        if os.getenv("IPA_PROVIDER_URL"):
            return super().resolve_ipa_url(bundle_id, version, timeout=timeout)

        entries = self._fetch_library(timeout)
        matches = [item for item in entries if self._app_matches(item, bundle_id)]
        if not matches:
            raise ProviderError(f"{bundle_id} не найден в Decrypt.day library")

        requested = str(version).strip().lower()
        if requested not in ("", "latest", "newest"):
            matches = [item for item in matches if str(item.get("version", "")) == str(version)]
            if not matches:
                raise ProviderError(f"Версия {version} для {bundle_id} не найдена в Decrypt.day library")
        else:
            matches.sort(key=lambda item: self._version_key(item.get("version", "0")), reverse=True)

        item = matches[0]
        ipa_url = item.get("link") or item.get("url") or item.get("download_url") or item.get("ipa_url")
        if not isinstance(ipa_url, str) or not ipa_url.startswith(("https://", "http://")):
            raise ProviderError("Запись Decrypt.day не содержит корректной ссылки на IPA")

        resolved_version = str(item.get("version", "")).strip()
        if not resolved_version:
            raise ProviderError("Запись Decrypt.day не содержит версии приложения")
        self.resolved_version = resolved_version
        return ipa_url
