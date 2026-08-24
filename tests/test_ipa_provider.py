import io
import plistlib
import zipfile

import pytest

from providers.base import MAX_IPA_SIZE, ProviderError
from providers.decrypt_ipa import DecryptIPASourceProvider


def mock_ipa(bundle_id, version):
    buf = io.BytesIO()
    plist = {"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version, "CFBundleVersion": "100"}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Payload/Test.app/Info.plist", plistlib.dumps(plist))
    return buf.getvalue()


def test_provider_success(httpx_mock, monkeypatch):
    monkeypatch.setenv("IPA_PROVIDER_URL", "https://api.example.test/resolve")
    httpx_mock.add_response(json={"download_url": "https://cdn.example.test/app.ipa"})
    data = mock_ipa("com.google.ios.youtube", "21.01.1")
    httpx_mock.add_response(content=data)
    result = DecryptIPASourceProvider().get_latest("com.google.ios.youtube", "21.01.1")
    assert result.verified and result.sha256


def test_api_retry(httpx_mock, monkeypatch):
    monkeypatch.setenv("IPA_PROVIDER_URL", "https://api.example.test/resolve")
    monkeypatch.setenv("IPA_PROVIDER_TIMEOUT", "1")
    httpx_mock.add_response(status_code=502)
    httpx_mock.add_response(json={"download_url": "https://cdn.example.test/app.ipa"})
    httpx_mock.add_response(content=mock_ipa("com.google.ios.youtube", "21.01.1"))
    result = DecryptIPASourceProvider().get_latest("com.google.ios.youtube", "21.01.1")
    assert result.verified


def test_bundle_id_mismatch(httpx_mock, monkeypatch):
    monkeypatch.setenv("IPA_PROVIDER_URL", "https://api.example.test/resolve")
    httpx_mock.add_response(json={"download_url": "https://cdn.example.test/app.ipa"})
    httpx_mock.add_response(content=mock_ipa("com.other.app", "21.01.1"))
    with pytest.raises(ProviderError, match="Bundle ID"):
        DecryptIPASourceProvider().get_latest("com.google.ios.youtube", "21.01.1")


def test_size_limit(httpx_mock, monkeypatch):
    monkeypatch.setenv("IPA_PROVIDER_URL", "https://api.example.test/resolve")
    httpx_mock.add_response(json={"download_url": "https://cdn.example.test/app.ipa"})
    httpx_mock.add_response(headers={"Content-Length": str(MAX_IPA_SIZE + 1)})
    with pytest.raises(ProviderError, match="лимит"):
        DecryptIPASourceProvider().get_latest("com.google.ios.youtube", "21.01.1")
