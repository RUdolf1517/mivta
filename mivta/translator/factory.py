"""Factory for constructing a Translator instance."""

from __future__ import annotations

import httpx

from mivta.translator.base import Translator, TranslatorError
from mivta.translator.deepl import DeepLTranslator
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
        headers={"User-Agent": "mivta/0.1"},
        follow_redirects=False,
    )


def create_translator(
    provider: str,
    client: httpx.AsyncClient,
    *,
    api_key: str = "",
    api_url: str = "",
    model: str = "gpt-4o-mini",
    max_length: int = 500,
) -> Translator:
    """Instantiate the requested translation provider."""
    normalized = provider.strip().lower()
    if normalized == "openai":
        return OpenAITranslator(
            client,
            api_key=api_key,
            model=model,
            base_url=api_url,
            max_length=max_length,
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
