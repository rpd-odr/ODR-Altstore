import os
import argparse
import json
import logging
from dataclasses import asdict

from providers.base import IPASourceProvider, IPAMetadata, ProviderError, sanitize_url
from providers.registry import ProviderRegistry, AdapterRegistry
import providers.adapters

logger = logging.getLogger("DecryptIPAProvider")


@ProviderRegistry.register("decrypt_ipa")
class DecryptIPASourceProvider(IPASourceProvider):
    def __init__(self, adapter=None):
        super().__init__()
        if adapter is not None:
            self.adapter = adapter
        else:
            name = os.getenv("IPA_PROVIDER_ADAPTER", "decryptipa")
            self.adapter = AdapterRegistry.get(name)()

    def get_latest(self, bundle_id: str, version: str = "latest", dry_run: bool = False, metadata_url: str = None, adapter_name: str = None) -> IPAMetadata:
        if adapter_name:
            self.adapter = AdapterRegistry.get(adapter_name)()
        requested_version = version
        logger.info("Запрос IPA: bundle_id=%s version=%s dry_run=%s adapter=%s", bundle_id, version, dry_run, type(self.adapter).__name__)

        meta = None
        if hasattr(self.adapter, "get_latest_metadata") and version in (None, "latest", "newest"):
            meta = self.adapter.get_latest_metadata(bundle_id, metadata_url=metadata_url)
            version = meta.get("version", "latest")

        try:
            ipa_url = self.adapter.resolve_ipa_url(bundle_id, version, timeout=self.timeout, metadata_url=metadata_url)
        except TypeError:
            ipa_url = self.adapter.resolve_ipa_url(bundle_id, version, timeout=self.timeout)

        if dry_run:
            display_version = version if version not in (None, "latest", "newest") else (meta or {}).get("version", "latest")
            logger.info("[DRY-RUN] Найден IPA URL: %s", sanitize_url(ipa_url))
            return IPAMetadata(bundle_id, display_version, (meta or {}).get("build", "dry-run"), ipa_url, "decrypt_ipa", 0, "dry_run", False)

        # For decrypt.day the page contains a short-lived download URL and may
        # not expose a machine-readable version. The IPA itself is authoritative.
        inspection = self.download_and_inspect_ipa(
            ipa_url,
            bundle_id,
            None if requested_version in (None, "latest", "newest") else requested_version,
        )
        return IPAMetadata(
            bundle_id=inspection["bundle_id"], version=inspection["version"], build=inspection["build"],
            ipa_url=ipa_url, source="decrypt_ipa", size=inspection["size"],
            sha256=inspection["sha256"], verified=True,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--version", default="latest")
    parser.add_argument("--metadata-url")
    parser.add_argument("--adapter")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    provider = None
    try:
        provider = ProviderRegistry.get("decrypt_ipa")
        result = provider.get_latest(args.bundle_id, args.version, args.dry_run, args.metadata_url, args.adapter)
        print(json.dumps(asdict(result), indent=2))
    except Exception as exc:
        logger.error("Ошибка выполнения: %s", exc)
        if provider:
            provider.log_gh_annotation("error", f"IPA Provider Error: {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
