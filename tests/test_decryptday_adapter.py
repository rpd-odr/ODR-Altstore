from providers.adapters.decryptday import DecryptDayAdapter


def test_extract_decryptday_download_path():
    page = '''<a href="/app/id544007664/dl/AbCdEf_123?download=1">Download IPA</a>'''
    url = DecryptDayAdapter._extract_download_url(
        "https://decrypt.day/app/id544007664", page
    )
    assert url == "https://decrypt.day/app/id544007664/dl/AbCdEf_123?download=1"


def test_extract_absolute_ipa_url():
    page = '''<script>const ipa = "https://cdn.decrypt.day/app.ipa?token=abc";</script>'''
    url = DecryptDayAdapter._extract_download_url(
        "https://decrypt.day/app/id1", page
    )
    assert url == "https://cdn.decrypt.day/app.ipa?token=abc"


def test_missing_download_url():
    assert DecryptDayAdapter._extract_download_url(
        "https://decrypt.day/app/id1", "Cloudflare challenge"
    ) is None
