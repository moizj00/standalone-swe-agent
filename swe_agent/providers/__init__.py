"""Cloud LLM providers (OpenAI-compatible APIs)."""
from .openai_compat import OpenAICompatibleProvider, to_openai_messages
from .registry import (
    CLOUD_PROVIDER_NAMES,
    PROVIDERS,
    ProviderSpec,
    check_cloud_provider,
    get_provider,
    is_cloud_provider,
    list_providers,
)

__all__ = [
    "OpenAICompatibleProvider",
    "to_openai_messages",
    "CLOUD_PROVIDER_NAMES",
    "PROVIDERS",
    "ProviderSpec",
    "check_cloud_provider",
    "get_provider",
    "is_cloud_provider",
    "list_providers",
]