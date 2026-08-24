import logging
import os
import time
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
        value = response.headers.get("Retry-After")
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    return max(0.0, (parsedate_to_datetime(value) - parsedate_to_datetime(response.headers.get("Date"))).total_seconds())
                except Exception:
                    pass
        return float(2 ** attempt)

    def resolve_ipa_url(self, bundle_id: str, version: str, timeout: float = 30.0) -> str:
        api_url = os.getenv("IPA_PROVIDER_URL")
        token = os.getenv("IPA_PROVIDER_TOKEN")
        if not api_url:
            raise ProviderError("Переменная окружения IPA_PROVIDER_URL не задана")

        headers = {"User-Agent": os.getenv("IPA_PROVIDER_USER_AGENT", "ODR-Alt-Repository-Provider/2.3")}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        params = {"bundle_id": bundle_id, "version": version}
        clean_url = sanitize_url(api_url)

        with httpx.Client(timeout=httpx.Timeout(timeout), headers=headers, follow_redirects=True) as client:
            response: Optional[httpx.Response] = None
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = client.get(api_url, params=params)
                    if response.status_code == 404:
                        raise ProviderError(f"Версия {version} для {bundle_id} не найдена во внешнем источнике")
                    if response.status_code in (429,) or response.status_code >= 500:
                        if attempt < self.max_retries:
                            delay = self._retry_delay(response, attempt)
                            logger.warning("API HTTP %s (%s), retry %s/%s через %.1fs", response.status_code, clean_url, attempt, self.max_retries, delay)
                            time.sleep(delay)
                            continue
                        raise ProviderError(f"API вернул HTTP {response.status_code} после {self.max_retries} попыток ({clean_url})")
                    response.raise_for_status()
                    break
                except httpx.RequestError as exc:
                    if attempt >= self.max_retries:
                        raise ProviderError(f"Ошибка сети API ({clean_url}): {exc}") from exc
                    time.sleep(2 ** attempt)

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
