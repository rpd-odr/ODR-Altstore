from providers.adapters.base import BaseDecryptAdapter
from providers.registry import AdapterRegistry

@AdapterRegistry.register("decryptipa")
class DecryptIPAAdapter(BaseDecryptAdapter):
    """Adapter boundary for the configured IPA source API."""
    pass
