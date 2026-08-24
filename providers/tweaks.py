import json
import os
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

import httpx


class TweakSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class TweakMetadata:
    id: str
    repo: str
    version: str
    tag: str
    commit: str
    release_url: str
    assets: List[Dict[str, str]]


def _github_headers() -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ODR-Alt-TweakProvider/1.0"}
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def resolve_latest(repo: str, tweak_id: str, timeout: float = 30.0) -> TweakMetadata:
    """Resolve the current GitHub release without storing a fixed tweak version."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True, headers=_github_headers()) as client:
            response = client.get(url)
            if response.status_code == 404:
                raise TweakSourceError(f"У репозитория {repo} нет latest release")
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise TweakSourceError(f"GitHub API error for {repo}: {exc}") from exc

    tag = str(data.get("tag_name") or "")
    if not tag:
        raise TweakSourceError(f"GitHub release для {repo} не содержит tag_name")

    assets = []
    for asset in data.get("assets", []):
        if isinstance(asset, dict) and asset.get("name") and asset.get("browser_download_url"):
            assets.append({"name": asset["name"], "url": asset["browser_download_url"]})

    return TweakMetadata(
        id=tweak_id,
        repo=repo,
        version=tag.lstrip("v"),
        tag=tag,
        commit=str(data.get("target_commitish") or ""),
        release_url=str(data.get("html_url") or f"https://github.com/{repo}/releases"),
        assets=assets,
    )


def load_tweak_config(path: str = "tweaks.json", builder: str = "ytkace") -> List[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return list(data.get(builder, {}).get("tweaks", []))


def resolve_builder_tweaks(builder: str, path: str = "tweaks.json") -> List[TweakMetadata]:
    result = []
    for item in load_tweak_config(path, builder):
        result.append(resolve_latest(item["repo"], item["id"]))
    return result


def write_github_output(tweaks: List[TweakMetadata]) -> None:
    output = os.getenv("GITHUB_OUTPUT")
    if not output:
        return
    payload = json.dumps([asdict(t) for t in tweaks], ensure_ascii=False, separators=(",", ":"))
    with open(output, "a", encoding="utf-8") as fh:
        fh.write("tweaks_json<<ODR_TWEAKS_EOF\n")
        fh.write(payload + "\nODR_TWEAKS_EOF\n")
        fh.write("tweak_count=" + str(len(tweaks)) + "\n")
