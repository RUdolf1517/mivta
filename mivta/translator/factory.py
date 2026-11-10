"""Factory for constructing a Translator instance (including LLM presets and fallbacks)."""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from mivta import __version__
from mivta.translator.base import LLM_PROVIDER_DEFAULTS, Translator, TranslatorError
from mivta.translator.deepl import DeepLTranslator
from mivta.translator.fallback import FallbackTranslator
from mivta.translator.google import GoogleTranslator
from mivta.translator.libre import LibreTranslateTranslator
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
    raise TranslatorError(f"Unknown translator provider: {provider!r}")


def create_translator(
    provider: str,
    client: httpx.AsyncClient,
    *,
    api_key: str = "",
    api_url: str = "",
    model: str = "",
    max_length: int = 500,
    glossary: Sequence[str] = (),
    fallback_providers: Sequence[str] = (),
    fallback_api_key: str = "",
    fallback_api_url: str = "",
    fallback_model: str = "",
) -> Translator:
    """Instantiate the requested translation provider and optional fallback chain."""
    primary = _create_single_translator(
        provider,
        client,
        api_key=api_key,
        api_url=api_url,
        model=model,
        max_length=max_length,
        glossary=glossary,
    )
    if not fallback_providers:
        return primary

    chain: list[Translator] = [primary]
    seen = {primary.name}
    for fb_name in fallback_providers:
        norm_fb = fb_name.strip().lower()
        if not norm_fb or norm_fb in seen:
            continue
        seen.add(norm_fb)
        chain.append(
            _create_single_translator(
                norm_fb,
                client,
                api_key=fallback_api_key,
                api_url=fallback_api_url,
                model=fallback_model,
                max_length=max_length,
                glossary=glossary,
            )
        )

    if len(chain) == 1:
        return primary
    return FallbackTranslator(chain)
