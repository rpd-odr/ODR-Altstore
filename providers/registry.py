import logging
from typing import Dict, Type
from providers.base import IPASourceProvider, ProviderError

logger = logging.getLogger("Registry")

class ProviderRegistry:
    _providers: Dict[str, Type[IPASourceProvider]] = {}
    @classmethod
    def register(cls, name: str):
        def decorator(subclass: Type[IPASourceProvider]):
            cls._providers[name] = subclass
            return subclass
        return decorator
    @classmethod
    def get(cls, name: str) -> IPASourceProvider:
        if name not in cls._providers:
            raise ProviderError(f"Провайдер '{name}' не зарегистрирован")
        return cls._providers[name]()

class AdapterRegistry:
    _adapters: Dict[str, Type] = {}
    @classmethod
    def register(cls, name: str):
        def decorator(subclass: Type):
            cls._adapters[name] = subclass
            return subclass
        return decorator
    @classmethod
    def get(cls, name: str):
        if name not in cls._adapters:
            raise ProviderError(f"Адаптер '{name}' не зарегистрирован")
        return cls._adapters[name]
