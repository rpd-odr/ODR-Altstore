#!/usr/bin/env python3
import json, urllib.request
from pathlib import Path

APPS = {
    "youtube": ("com.google.ios.youtube", "YouTube"),
    "instagram": ("com.burbn.instagram", "Instagram"),
}

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ODR-Alt-Version-Watcher"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

out = {}
for key, (bundle, name) in APPS.items():
    data = get(f"https://itunes.apple.com/lookup?bundleId={bundle}&country=us")
    if not data.get("results"):
        print(f"{name}: not found")
        continue
    app = data["results"][0]
    out[key] = {
        "name": name,
        "bundleIdentifier": bundle,
        "version": app.get("version"),
        "trackId": app.get("trackId"),
        "releaseDate": app.get("releaseDate"),
        "storeURL": app.get("trackViewUrl"),
    }
    print(f"{name}: {app.get('version')}")

Path("metadata").mkdir(exist_ok=True)
Path("metadata/store-versions.json").write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
