import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import httpx

from providers.base import ProviderError, sanitize_url

logger = logging.getLogger("BaseDecryptAdapter")


class BaseDecryptAdapter:
    def __init__(self, max_retries: int = 3):
        self.max_retries = max(1, max_retries)

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        """Honor Retry-After when supplied, otherwise use exponential backoff."""
        value = response.headers.get("Retry-After")
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(2 ** attempt)

    def _request_with_retry(
        self,
        client: httpx.Client,
        url: str,
        method: str = "GET",
        headers: Optional[dict] = None,
        **kwargs
    ) -> httpx.Response:
        """Make an HTTP request with retry logic."""
        clean_url = sanitize_url(url)
        
        for attempt in range(1, self.max_retries + 1):
            try:
                response = client.request(method, url, headers=headers, **kwargs)
                
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        delay = self._retry_delay(response, attempt)
                        logger.warning(
                            "HTTP %s (%s), retry %s/%s через %.1fs",
                            response.status_code,
                            clean_url,
                            attempt,
                            self.max_retries,
                            delay,
                        )
                        time.sleep(delay)
                        continue
                
                response.raise_for_status()
                return response
            except httpx.RequestError as exc:
                if attempt >= self.max_retries:
                    raise ProviderError(f"Ошибка сети ({clean_url}): {exc}") from exc
                delay = float(2 ** attempt)
                logger.warning(
                    "Ошибка сети (%s), retry %s/%s через %.1fs",
                    clean_url,
                    attempt,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)
        
        raise ProviderError(f"Не удалось получить ответ от сервера ({clean_url})")

    def resolve_ipa_url(self, bundle_id: str, version: str, timeout: float = 30.0) -> str:
        api_url = os.getenv("IPA_PROVIDER_URL")
        token = os.getenv("IPA_PROVIDER_TOKEN")
        if not api_url:
            raise ProviderError("Переменная окружения IPA_PROVIDER_URL не задана")

        headers = {"User-Agent": os.getenv("IPA_PROVIDER_USER_AGENT", "ODR-Alt-Repository-Provider/2.4")}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        params = {"bundle_id": bundle_id, "version": version}
        clean_url = sanitize_url(api_url)

        with httpx.Client(timeout=httpx.Timeout(timeout), headers=headers, follow_redirects=True) as client:
            response: Optional[httpx.Response] = None
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = client.get(api_url, params=params, headers=headers)

                    if response.status_code == 404:
                        raise ProviderError(
                            f"Версия {version} для {bundle_id} не найдена во внешнем источнике"
                        )

                    if response.status_code == 429 or response.status_code >= 500:
                        if attempt < self.max_retries:
                            delay = self._retry_delay(response, attempt)
                            logger.warning(
                                "API HTTP %s (%s), retry %s/%s через %.1fs",
                                response.status_code,
                                clean_url,
                                attempt,
                                self.max_retries,
                                delay,
                            )
                            time.sleep(delay)
                            continue
                        raise ProviderError(
                            f"API вернул HTTP {response.status_code} после "
                            f"{self.max_retries} попыток ({clean_url})"
                        )

                    response.raise_for_status()
                    break
                except httpx.RequestError as exc:
                    if attempt >= self.max_retries:
                        raise ProviderError(f"Ошибка сети API ({clean_url}): {exc}") from exc
                    delay = float(2 ** attempt)
                    logger.warning(
                        "Ошибка сети API (%s), retry %s/%s через %.1fs",
                        clean_url,
                        attempt,
                        self.max_retries,
                        delay,
                    )
                    time.sleep(delay)

            if response is None:
                raise ProviderError(f"Не удалось получить ответ от API ({clean_url})")

            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderError(f"API вернул некорректный JSON ({clean_url})") from exc

            if not isinstance(data, dict):
                raise ProviderError("Ответ API должен быть JSON-объектом")

            ipa_url = data.get("download_url") or data.get("ipa_url")
            if not isinstance(ipa_url, str) or not ipa_url.startswith(("https://", "http://")):
                raise ProviderError("Ответ API не содержит корректного download_url/ipa_url")
            return ipa_url
