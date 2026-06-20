"""Built-in cloud provider presets (OpenAI-compatible APIs)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

NEMOTRON_DUMMY_KEY = "dummy-key"


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    base_url: str
    default_model: str
    api_key_env: str
    api_key_env_aliases: Tuple[str, ...] = ()
    dummy_key_fallback: bool = False

    def resolve_api_key(self) -> str:
        for env in (self.api_key_env, *self.api_key_env_aliases):
            val = os.environ.get(env, "").strip()
            if val:
                return val
        if self.dummy_key_fallback:
            return NEMOTRON_DUMMY_KEY
        return ""


PROVIDERS: Dict[str, ProviderSpec] = {
    "minimax": ProviderSpec(
        name="minimax",
        label="MiniMax",
        base_url=os.environ.get("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/"),
        default_model=os.environ.get("MINIMAX_MODEL", "MiniMax-M2.5"),
        api_key_env="MINIMAX_API_KEY",
    ),
    "kimi": ProviderSpec(
        name="kimi",
        label="Kimi (Moonshot)",
        base_url=os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1").rstrip("/"),
        default_model=os.environ.get("KIMI_MODEL", "kimi-k2.7-code"),
        api_key_env="MOONSHOT_API_KEY",
        api_key_env_aliases=("KIMI_API_KEY",),
    ),
    "nemotron": ProviderSpec(
        name="nemotron",
        label="NVIDIA Nemotron (NIM)",
        base_url=os.environ.get(
            "NEMOTRON_BASE_URL", "https://integrate.api.nvidia.com/v1"
        ).rstrip("/"),
        default_model=os.environ.get(
            "NEMOTRON_MODEL", "nvidia/nemotron-4-340b-instruct"
        ),
        api_key_env="NVIDIA_API_KEY",
        api_key_env_aliases=("NVIDIA_NIM_API_KEY", "NGC_API_KEY"),
        dummy_key_fallback=True,
    ),
    "openai": ProviderSpec(
        name="openai",
        label="OpenAI",
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        default_model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        api_key_env="OPENAI_API_KEY",
    ),
}

CLOUD_PROVIDER_NAMES = tuple(PROVIDERS.keys())


def get_provider(name: str) -> Optional[ProviderSpec]:
    return PROVIDERS.get((name or "").lower())


def list_providers() -> List[ProviderSpec]:
    return list(PROVIDERS.values())


def is_cloud_provider(name: str) -> bool:
    return (name or "").lower() in PROVIDERS


def check_cloud_provider(name: str) -> Tuple[bool, str]:
    spec = get_provider(name)
    if spec is None:
        return False, f"Unknown cloud provider '{name}'. Choose: {', '.join(CLOUD_PROVIDER_NAMES)}."
    key = spec.resolve_api_key()
    if not key:
        aliases = ", ".join((spec.api_key_env, *spec.api_key_env_aliases))
        return False, (
            f"{spec.label} requires an API key. Set one of: {aliases}."
        )
    hint = ""
    if spec.dummy_key_fallback and key == NEMOTRON_DUMMY_KEY:
        hint = " (using placeholder key — set NVIDIA_API_KEY for live calls)"
    return True, f"ok ({spec.label}, model default: {spec.default_model}){hint}"