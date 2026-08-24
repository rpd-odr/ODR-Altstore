import json

from providers.base import IPAMetadata
from watcher import on_new_version_detected


def test_watcher_preserves_existing_state(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sources.json").write_text(
        json.dumps({
            "sources": [{
                "builder": "ytkace",
                "bundleIdentifier": "com.google.ios.youtube",
                "provider": "decrypt_ipa",
            }]
        }),
        encoding="utf-8",
    )
    (tmp_path / "state.json").write_text(
        json.dumps({
            "ytkace": {
                "upstream": "itzzace/ytkace@old",
                "tag": "v0.8.0",
            },
            "termix": {"upstream": "Termix-SSH/Mobile@old"},
        }),
        encoding="utf-8",
    )

    class FakeProvider:
        def get_latest(self, *args, **kwargs):
            return IPAMetadata(
                bundle_id="com.google.ios.youtube",
                version="21.33.6",
                build="123",
                ipa_url="https://example.test/youtube.ipa",
                source="decrypt_ipa",
                size=123,
                sha256="abc123",
                verified=True,
            )

    monkeypatch.setattr("watcher.ProviderRegistry.get", lambda _: FakeProvider())
    on_new_version_detected("ytkace")

    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["termix"]["upstream"] == "Termix-SSH/Mobile@old"
    assert state["ytkace"]["tag"] == "v0.8.0"
    assert state["ipa"]["ytkace"]["version"] == "21.33.6"
    assert state["ipa"]["ytkace"]["sha256"] == "abc123"
