# IPA provider

The provider layer resolves an IPA URL through a configured adapter, downloads it with a 2 GiB hard limit, verifies the ZIP, extracts `Payload/*.app/Info.plist`, and checks Bundle ID and version before a builder can consume it.

## Configuration

- `IPA_PROVIDER_URL` — API endpoint returning `download_url` or `ipa_url`.
- `IPA_PROVIDER_TOKEN` — optional bearer token, supplied through GitHub Actions secrets.
- `IPA_PROVIDER_ADAPTER` — adapter registry name; defaults to `decryptipa`.
- `IPA_PROVIDER_TIMEOUT` — HTTP timeout in seconds; defaults to 30.

The adapter boundary is intentionally generic. The concrete `DecryptIPAAdapter` is currently a thin adapter and must be extended with source-specific request/response handling when the target source contract is known.

## Safety properties

- No fixed temporary filename.
- 2 GiB Content-Length and streaming limits.
- SHA-256 calculated while streaming.
- ZIP integrity and Info.plist validation.
- URLs are sanitized in logs.
- 429/5xx responses retry with exponential backoff and honor `Retry-After` when usable.
- Unknown adapters fail closed instead of silently falling back.

## Tests

```bash
python -m pip install -r requirements.txt
pytest -q
```
