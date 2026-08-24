import json
import logging
import os
from typing import Dict, Any

from providers.registry import ProviderRegistry
from providers.utils import load_state, save_state_atomic

logger = logging.getLogger("Watcher")
SOURCES_FILE = "sources.json"

def load_sources_config() -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(SOURCES_FILE):
        raise FileNotFoundError(f"Не найден {SOURCES_FILE}")
    with open(SOURCES_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data.get("apps", data.get("sources", []))
    if not isinstance(entries, list):
        raise ValueError(f"{SOURCES_FILE}: ожидается массив apps или sources")
    result = {}
    for app in entries:
        if not isinstance(app, dict) or not app.get("builder") or not app.get("bundleIdentifier"):
            continue
        result[app["builder"]] = {
            "provider": app.get("provider", "decrypt_ipa"),
            "bundle_id": app["bundleIdentifier"],
            "id": app.get("id", app["builder"]),
            "name": app.get("name", app["builder"]),
        }
    return result

def on_new_version_detected(builder_name: str, detected_version: str, dry_run: bool = False):
    config = load_sources_config().get(builder_name)
    if not config:
        raise ValueError(f"Builder '{builder_name}' не найден в {SOURCES_FILE}")
    provider = ProviderRegistry.get(config["provider"])
    metadata = provider.get_latest(config["bundle_id"], detected_version, dry_run=dry_run)
    if not dry_run and not metadata.verified:
        raise RuntimeError(f"IPA для {builder_name} не прошёл валидацию")

    state = load_state()
    state[builder_name] = {
        "bundle_id": metadata.bundle_id,
        "version": metadata.version,
        "build": metadata.build,
        "sha256": metadata.sha256,
        "size": metadata.size,
        "verified": metadata.verified,
    }
    save_state_atomic(state)

    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"ipa_url={metadata.ipa_url}\n")
            fh.write(f"verified={str(metadata.verified).lower()}\n")
            fh.write(f"version={metadata.version}\n")
            fh.write(f"build={metadata.build}\n")
            fh.write(f"sha256={metadata.sha256}\n")
    return metadata
