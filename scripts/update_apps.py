import json
import os
import plistlib
import subprocess
import tempfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources.json"
APPS = ROOT / "apps.json"
STATE = ROOT / "state.json"
OWNER = os.environ.get("GITHUB_REPOSITORY", "rpd-odr/odr-alt")
TOKEN = os.environ.get("GH_TOKEN", "")
HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "odr-alt-updater",
}
if TOKEN:
    HEADERS["Authorization"] = f"Bearer {TOKEN}"


def github_json(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def gh(*args):
    return subprocess.run(["gh", *args], check=True, text=True, capture_output=True).stdout.strip()


def release(repo):
    return github_json(f"https://api.github.com/repos/{repo}/releases/latest")


def ipa_asset(rel, pattern):
    import re
    rx = re.compile(pattern, re.I)
    for asset in rel.get("assets", []):
        if rx.search(asset.get("name", "")):
            return asset
    return None


def safe_tag(app_id, tag):
    value = f"mirror-{app_id}-{tag}"
    return "".join(c if c.isalnum() or c in "._-" else "-" for c in value)[:120]


def existing_release(tag):
    p = subprocess.run(["gh", "release", "view", tag, "--repo", OWNER], text=True, capture_output=True)
    return p.returncode == 0


def inspect_ipa(path):
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            candidates = [n for n in z.namelist() if n.endswith(".app/Info.plist")]
            if not candidates:
                return {}
            with z.open(candidates[0]) as f:
                return plistlib.loads(f.read())
    except Exception as e:
        print(f"IPA metadata warning: {e}")
        return {}


def mirror(src, rel, asset):
    tag = safe_tag(src["id"], rel["tag_name"])
    filename = asset["name"]
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / filename
        print(f"Downloading {src['id']}: {asset['name']}")
        with requests.get(asset["browser_download_url"], headers=HEADERS, stream=True, timeout=120) as r:
            r.raise_for_status()
            with path.open("wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)

        info = inspect_ipa(path)
        if info:
            print(f"IPA: {info.get('CFBundleIdentifier')} {info.get('CFBundleShortVersionString')} ({info.get('CFBundleVersion')})")

        if not existing_release(tag):
            subprocess.run([
                "gh", "release", "create", tag, str(path),
                "--repo", OWNER,
                "--title", f"{src['name']} {rel['tag_name']}",
                "--notes", f"Mirrored from {src['repo']} release {rel['tag_name']}.\n\nUpstream: https://github.com/{src['repo']}/releases/tag/{rel['tag_name']}",
            ], check=True)
        else:
            subprocess.run(["gh", "release", "upload", tag, str(path), "--repo", OWNER, "--clobber"], check=True)

    return f"https://github.com/{OWNER}/releases/download/{tag}/{filename}", info


def main():
    cfg = json.loads(SOURCES.read_text())
    source = json.loads(APPS.read_text()) if APPS.exists() else {
        "name": "ODR Altstore",
        "identifier": "su.odr.altstore",
        "subtitle": "ODR apps for SideStore and LiveContainer",
        "description": "A curated source of iOS apps distributed by their respective authors.",
        "iconURL": "https://raw.githubusercontent.com/rpd-odr/odr-alt/main/icon.png",
        "website": "https://github.com/rpd-odr/odr-alt",
        "apps": [],
    }
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    apps = {a["bundleIdentifier"]: a for a in source.get("apps", [])}

    for src in cfg["sources"]:
        try:
            rel = release(src["repo"])
            asset = ipa_asset(rel, src.get("assetPattern", r"\.ipa$"))
            if not asset:
                print(f"{src['id']}: latest release {rel.get('tag_name')} has no IPA asset; skipped")
                continue
            upstream_key = f"{src['repo']}@{rel['id']}"
            old = state.get(src["id"], {}).get("upstream")
            if old == upstream_key and apps.get(src["bundleIdentifier"], {}).get("downloadURL", "").startswith(f"https://github.com/{OWNER}/releases/"):
                print(f"{src['id']}: already mirrored {rel['tag_name']}")
                continue

            url, info = mirror(src, rel, asset)
            app = {
                "name": src["name"],
                "bundleIdentifier": src["bundleIdentifier"],
                "developerName": src["developerName"],
                "subtitle": src["subtitle"],
                "localizedDescription": src["localizedDescription"],
                "iconURL": src.get("iconURL", ""),
                "downloadURL": url,
                "version": (info.get("CFBundleShortVersionString") if info else None) or rel["tag_name"].lstrip("v"),
                "buildVersion": (info.get("CFBundleVersion") if info else None) or "",
                "versionDate": (rel.get("published_at") or "")[:10],
                "versionDescription": rel.get("body") or "",
                "size": asset.get("size", 0),
            }
            apps[src["bundleIdentifier"]] = app
            state[src["id"]] = {"upstream": upstream_key, "tag": rel["tag_name"], "mirrorTag": safe_tag(src["id"], rel["tag_name"]), "asset": asset["name"]}
        except Exception as e:
            print(f"ERROR {src['id']}: {e}")

    source["iconURL"] = "https://raw.githubusercontent.com/rpd-odr/odr-alt/main/icon.png"
    source["website"] = "https://github.com/rpd-odr/odr-alt"
    source["apps"] = list(apps.values())
    APPS.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n")
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
