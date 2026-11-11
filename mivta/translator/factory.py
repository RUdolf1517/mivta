"""Factory for constructing a Translator instance (including LLM presets and failover chains)."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

import httpx

from mivta import __version__
from mivta.translator.base import (
    LLM_PROVIDER_DEFAULTS,
    ProviderSpec,
    Translator,
    TranslatorError,
)
from mivta.translator.deepl import DeepLTranslator
from mivta.translator.fallback import (
    DEFAULT_UNAVAILABLE_COOLDOWN,
    FallbackTranslator,
)
from mivta.translator.google import GoogleTranslator
from mivta.translator.libre import LibreTranslateTranslator
from mivta.translator.mymemory import MyMemoryTranslator
from mivta.translator.openai import OpenAITranslator


def create_http_client(timeout_seconds: float) -> httpx.AsyncClient:
    """Create a shared async HTTP client with explicit timeouts and bounded pool."""
    connect_timeout = min(3.0, timeout_seconds)
    timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout)
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        headers={"User-Agent": f"mivta/{__version__}"},
        follow_redirects=False,
    )


def _create_single_translator(
    provider: str,
    client: httpx.AsyncClient,
    *,
    api_key: str = "",
    api_url: str = "",
    model: str = "",
    email: str = "",
    max_length: int = 500,
    glossary: Sequence[str] = (),
) -> Translator:
    normalized = provider.strip().lower()
    if normalized in LLM_PROVIDER_DEFAULTS:
        preset_url, preset_model = LLM_PROVIDER_DEFAULTS[normalized]
        return OpenAITranslator(
            client,
            api_key=api_key,
            model=model or preset_model,
            base_url=api_url or preset_url,
            max_length=max_length,
            provider_name=normalized,
            glossary=glossary,
        )
    if normalized == "deepl":
        return DeepLTranslator(
            client,
            api_key=api_key,
            base_url=api_url,
            max_length=max_length,
        )
    if normalized == "libretranslate":
        return LibreTranslateTranslator(
            client,
            base_url=api_url,
            api_key=api_key,
            max_length=max_length,
        )
    if normalized == "google":
        return GoogleTranslator(
            client,
            base_url=api_url,
            max_length=max_length,
        )
    if normalized == "mymemory":
        return MyMemoryTranslator(
            client,
            base_url=api_url,
            api_key=api_key,
            email=email,
            max_length=max_length,
        )
    raise TranslatorError(f"Unknown translator provider: {provider!r}")


def create_translator(
    provider: str,
    client: httpx.AsyncClient,
    *,
    api_key: str = "",
    api_url: str = "",
    model: str = "",
    email: str = "",
    max_length: int = 500,
    glossary: Sequence[str] = (),
    fallback_providers: Sequence[str] = (),
    fallback_api_key: str = "",
    fallback_api_url: str = "",
    fallback_model: str = "",
    provider_specs: Sequence[ProviderSpec] = (),
    cooldown_seconds: float = DEFAULT_UNAVAILABLE_COOLDOWN,
    clock: Callable[[], float] = time.monotonic,
) -> Translator:
    """Instantiate the requested translation provider or multi-API failover chain."""
    if provider_specs:
        specs = list(provider_specs)
    else:
        specs = [
            ProviderSpec(
                provider=provider,
                api_key=api_key,
                api_url=api_url,
                model=model,
                email=email,
            )
        ]
        for fb_name in fallback_providers:
            specs.append(
                ProviderSpec(
                    provider=fb_name,
                    api_key=fallback_api_key,
                    api_url=fallback_api_url,
                    model=fallback_model,
                )
            )

    chain: list[Translator] = []
    seen: set[str] = set()
    for spec in specs:
        norm = spec.provider.strip().lower()
        if not norm or norm in seen:
            continue
        seen.add(norm)
        chain.append(
            _create_single_translator(
                norm,
                client,
                api_key=spec.api_key,
                api_url=spec.api_url,
                model=spec.model,
                email=spec.email,
                max_length=max_length,
                glossary=glossary,
            )
        )

    if not chain:
        raise TranslatorError(f"Unknown translator provider: {provider!r}")
    if len(chain) == 1:
        return chain[0]

    base_cd = max(0.0, cooldown_seconds)
    return FallbackTranslator(
        chain,
        unavailable_cooldown=base_cd,
        rate_limit_cooldown=base_cd * 2.0,
        auth_cooldown=base_cd * 10.0,
        clock=clock,
    )
