import json
import os
import re
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / "apps.json"
OWNER = os.environ.get("GITHUB_REPOSITORY", "rpd-odr/odr-alt")
TOKEN = os.environ.get("GH_TOKEN", "")
HEADERS = {"Accept": "application/vnd.github+json", "User-Agent": "odr-alt-builder-source"}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"

BUILDERS = {
    "ytkace": {
        "name": "YTKACE", "bundleIdentifier": "com.google.ios.youtube", "developerName": "itzzace",
        "subtitle": "YouTube tweak build", "prefix": "ytkace-"
    },
    "ryukgram": {
        "name": "RyukGram", "bundleIdentifier": "com.burbn.instagram", "developerName": "faroukbmiled",
        "subtitle": "Instagram tweak build", "prefix": "ryukgram-"
    }
}

def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def main():
    source = json.loads(APPS.read_text())
    apps = {a["bundleIdentifier"]: a for a in source.get("apps", [])}
    releases = get(f"https://api.github.com/repos/{OWNER}/releases?per_page=100")
    for key, cfg in BUILDERS.items():
        matches = [r for r in releases if not r.get("draft") and r.get("tag_name", "").startswith(cfg["prefix"])]
        matches.sort(key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
        if not matches:
            apps.pop(cfg["bundleIdentifier"], None)
            print(f"{key}: no ODR build release yet")
            continue
        rel = matches[0]
        ipa = next((a for a in rel.get("assets", []) if a.get("name", "").lower().endswith(".ipa")), None)
        if not ipa:
            print(f"{key}: release {rel['tag_name']} has no IPA")
            continue
        filename = ipa["name"]
        version = re.search(r"(?:YouTube_|Instagram_)?([0-9]+(?:\.[0-9]+)+)", filename)
        version = version.group(1) if version else rel["tag_name"].removeprefix(cfg["prefix"])
        apps[cfg["bundleIdentifier"]] = {
            "name": cfg["name"], "bundleIdentifier": cfg["bundleIdentifier"], "developerName": cfg["developerName"],
            "subtitle": cfg["subtitle"], "localizedDescription": f"ODR build from {cfg['name']} upstream project.", "iconURL": "",
            "downloadURL": ipa["browser_download_url"], "version": version,
            "buildVersion": version, "versionDate": (rel.get("published_at") or "")[:10],
            "versionDescription": rel.get("body") or "", "size": ipa.get("size", 0)
        }
        print(f"{key}: {version} -> {ipa['name']}")
    source["apps"] = list(apps.values())
    APPS.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n")

if __name__ == "__main__":
    main()
