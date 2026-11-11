"""Tests for circuit-breaker failover cooldowns, MyMemoryTranslator, and multi-key chains."""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

import httpx
import pytest

from mivta.bot import configure_logging
from mivta.config import Config, ConfigError
from mivta.translator import (
    FallbackTranslator,
    GoogleTranslator,
    MyMemoryTranslator,
    OpenAITranslator,
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorAuthError,
    TranslatorError,
    TranslatorRateLimitError,
    TranslatorTimeoutError,
    TranslatorUnavailableError,
    UnsupportedLanguageError,
    create_translator,
)


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ToggleProvider(Translator):
    def __init__(
        self,
        name: str,
        *,
        supports_styles: bool = False,
        error: TranslatorError | None = None,
    ) -> None:
        self.name = name
        self.supports_styles = supports_styles
        self.error = error
        self.calls = 0
        self.style_calls = 0

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return TranslationResult(
            translated_text=f"{self.name}:{request.text}",
            source_lang="ru",
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )

    async def translate_styles(
        self,
        request: TranslationRequest,
        styles: list[TranslationStyle],
    ) -> list[TranslationResult]:
        self.style_calls += 1
        if self.error is not None:
            raise self.error
        return [
            TranslationResult(
                translated_text=f"{self.name}:{st.value}:{request.text}",
                source_lang="ru",
                target_lang=request.target_lang,
                style=st,
                provider=self.name,
            )
            for st in styles
        ]


async def test_circuit_breaker_cooldown_skips_unhealthy_primary_and_self_heals() -> None:
    clock = FakeClock()
    p_groq = ToggleProvider("groq", supports_styles=True, error=TranslatorRateLimitError("429"))
    p_gemini = ToggleProvider("gemini", supports_styles=True)

    chain = FallbackTranslator(
        [p_groq, p_gemini],
        unavailable_cooldown=30.0,
        rate_limit_cooldown=60.0,
        auth_cooldown=300.0,
        clock=clock,
    )

    # 1st call: groq fails with 429 -> enters 60s cooldown, gemini serves request
    r1 = await chain.translate(TranslationRequest(text="Привет", target_lang="en"))
    assert r1.provider == "gemini"
    assert p_groq.calls == 1 and p_gemini.calls == 1
    assert chain.is_cooling_down("groq") is True
    assert chain.is_cooling_down("gemini") is False
    assert chain.is_cooling_down("nonexistent") is False

    # 2nd & 3rd calls (30s later, still within 60s rate-limit cooldown):
    # groq is NOT called at all (0ms skip)!
    clock.advance(30.0)
    r2 = await chain.translate(TranslationRequest(text="Мир", target_lang="en"))
    assert r2.provider == "gemini"
    st = await chain.translate_styles(
        TranslationRequest(text="Тест", target_lang="en"),
        [TranslationStyle.NEUTRAL, TranslationStyle.FORMAL],
    )
    assert [x.provider for x in st] == ["gemini", "gemini"]
    assert p_groq.calls == 1 and p_groq.style_calls == 0
    assert p_gemini.calls == 2 and p_gemini.style_calls == 1

    # Advance past 60s cooldown -> groq becomes healthy again and automatically resumes primary!
    p_groq.error = None
    clock.advance(31.0)
    assert chain.is_cooling_down("groq") is False
    r3 = await chain.translate(TranslationRequest(text="Снова в строю", target_lang="en"))
    assert r3.provider == "groq"
    assert p_groq.calls == 2
    assert p_gemini.calls == 2


async def test_cooldown_tiers_unsupported_lang_no_cooldown_and_all_cooling_fallback() -> None:
    clock = FakeClock()
    p1 = ToggleProvider("p1", error=UnsupportedLanguageError("rare lang"))
    p2 = ToggleProvider("p2")
    chain = FallbackTranslator([p1, p2], clock=clock)

    # UnsupportedLanguageError fails over for this request WITHOUT putting p1 into cooldown
    res = await chain.translate(TranslationRequest(text="Привет", target_lang="en"))
    assert res.provider == "p2"
    assert chain.is_cooling_down("p1") is False

    # Auth error puts p1 into 300s cooldown; timeout puts p2 into 30s cooldown
    p1.error = TranslatorAuthError("401")
    p2.error = TranslatorTimeoutError("timeout")
    with pytest.raises(TranslatorTimeoutError):
        await chain.translate(TranslationRequest(text="Привет", target_lang="en"))
    assert chain.is_cooling_down("p1") is True
    assert chain.is_cooling_down("p2") is True

    # Even when ALL providers are cooling down, a retry still attempts them (earliest expiry first)
    p2.error = None
    recovered = await chain.translate(TranslationRequest(text="Привет", target_lang="en"))
    assert recovered.provider == "p2"
    assert chain.is_cooling_down("p2") is False
    assert chain.is_cooling_down("p1") is True

    chain.reset_cooldowns()
    assert chain.is_cooling_down("p1") is False


async def test_mymemory_translator_happy_path_entities_email_and_error_cases() -> None:
    seen_params: list[dict[str, str]] = []
    queued: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_params.append(dict(req.url.params))
        return httpx.Response(200, json=queued.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        mm = MyMemoryTranslator(client, email="bot@example.com")

        # 1. Explicit source == target skips network completely
        same = await mm.translate(
            TranslationRequest(text="Same", source_lang="en", target_lang="en")
        )
        assert same.translated_text == "Same" and len(seen_params) == 0

        # 2. Unique script (Hebrew -> he) inferred locally without autodetect overhead + de=email
        queued.append(
            {
                "responseStatus": 200,
                "responseData": {"translatedText": "It&#39;s &lt;s0&gt;fine&lt;/s0&gt;"},
            }
        )
        he_res = await mm.translate(
            TranslationRequest(text="שלום <s0>חבר</s0>", source_lang="auto", target_lang="en")
        )
        assert seen_params[-1]["langpair"] == "he|en"
        assert seen_params[-1]["de"] == "bot@example.com"
        assert he_res.source_lang == "he"
        assert he_res.translated_text == "It's <s0>fine</s0>"

        # 3. Literal `&amp;` in source code is NOT corrupted by entity unescaping
        mm_key = MyMemoryTranslator(client, api_key="mm_secret_key")
        queued.append(
            {
                "responseStatus": "200",
                "detectedLanguage": "ru-RU",
                "responseData": {"translatedText": "Use &amp;&amp; here"},
            }
        )
        amp_res = await mm_key.translate(
            TranslationRequest(text="Используй &amp;&amp; тут", target_lang="en")
        )
        assert seen_params[-1]["langpair"] == "autodetect|en"
        assert seen_params[-1]["key"] == "mm_secret_key"
        assert amp_res.source_lang == "ru"
        assert amp_res.translated_text == "Use &amp;&amp; here"

        # 4. Email passed via api_key (`user@domain`) automatically maps to `de=`
        mm_email_in_key = MyMemoryTranslator(client, api_key="ops@example.org")
        queued.append(
            {"responseStatus": 200, "responseData": {"translatedText": "Bonjour"}}
        )
        await mm_email_in_key.translate(TranslationRequest(text="Hello", target_lang="fr"))
        assert seen_params[-1]["de"] == "ops@example.org"
        assert "key" not in seen_params[-1]

        # 5. Same-language autodetect ("PLEASE SELECT TWO DISTINCT LANGUAGES") returns input safely
        queued.append(
            {
                "responseStatus": 403,
                "responseDetails": "PLEASE SELECT TWO DISTINCT LANGUAGES",
                "responseData": {"translatedText": "PLEASE SELECT TWO DISTINCT LANGUAGES"},
            }
        )
        distinct = await mm.translate(TranslationRequest(text="Hello", target_lang="en"))
        assert distinct.translated_text == "Hello"
        assert distinct.source_lang == "en"

        # 6. Quota warning / 429 raises TranslatorRateLimitError
        queued.append(
            {
                "responseStatus": 429,
                "responseDetails": "MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS",
                "responseData": {"translatedText": ""},
            }
        )
        with pytest.raises(TranslatorRateLimitError):
            await mm.translate(TranslationRequest(text="Привет", target_lang="en"))

        # 7. Invalid language pair raises UnsupportedLanguageError
        queued.append(
            {
                "responseStatus": 403,
                "responseDetails": "'zz' IS AN INVALID SOURCE LANGUAGE",
                "responseData": {},
            }
        )
        with pytest.raises(UnsupportedLanguageError):
            await mm.translate(
                TranslationRequest(text="Привет", source_lang="zz", target_lang="en")
            )

        # 8. Rejected credentials (403) and generic upstream errors / blank text
        queued.append(
            {"responseStatus": 403, "responseDetails": "INVALID EMAIL", "responseData": {}}
        )
        with pytest.raises(TranslatorAuthError):
            await mm.translate(TranslationRequest(text="Привет", target_lang="en"))

        queued.append({"responseStatus": 500, "responseData": {}})
        with pytest.raises(TranslatorUnavailableError):
            await mm.translate(TranslationRequest(text="Привет", target_lang="en"))

        queued.append({"responseStatus": 200, "responseData": {"translatedText": "   "}})
        with pytest.raises(TranslatorUnavailableError):
            await mm.translate(TranslationRequest(text="Привет", target_lang="en"))


async def test_multi_key_translator_chain_default_zero_key_failover_and_redaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 1. Out-of-the-box zero-key config automatically enables `google -> mymemory`
    default_cfg = Config.from_env(env={"BOT_TOKEN": "123:TOK"}, dotenv_path=None)
    assert default_cfg.translator_provider == "google"
    assert default_cfg.translator_fallback_providers == ("mymemory",)

    # Explicit `none` disables automatic fallback
    no_fb_cfg = Config.from_env(
        env={"BOT_TOKEN": "123:TOK", "TRANSLATOR_FALLBACK_PROVIDER": "none"},
        dotenv_path=None,
    )
    assert no_fb_cfg.translator_fallback_providers == ()

    # 2. Multi-key TRANSLATOR_CHAIN with per-provider keys (groq -> mistral -> google -> mymemory)
    chain_cfg = Config.from_env(
        env={
            "BOT_TOKEN": "123:BOT_SECRET",
            "TRANSLATOR_CHAIN": "groq, mistral, google, mymemory",
            "GROQ_API_KEY": "gsk_secret_1111",
            "MISTRAL_API_KEY": "mst_secret_2222",
            "MYMEMORY_EMAIL": "priv_mail@example.com",
            "TRANSLATOR_COOLDOWN": "45",
        },
        dotenv_path=None,
    )
    assert chain_cfg.translator_provider == "groq"
    assert chain_cfg.translator_fallback_providers == ("mistral", "google", "mymemory")
    assert chain_cfg.translator_cooldown == 45.0
    assert [s.provider for s in chain_cfg.translator_chain_specs] == [
        "groq",
        "mistral",
        "google",
        "mymemory",
    ]
    # Groq/Mistral keys NEVER leak into Google or MyMemory specs
    assert chain_cfg.translator_chain_specs[2].api_key == ""
    assert chain_cfg.translator_chain_specs[3].api_key == ""
    assert chain_cfg.translator_chain_specs[3].email == "priv_mail@example.com"

    # End-to-end 4-provider failover over HTTP + sticky circuit-breaker skip on 2nd call
    clock = FakeClock()
    hit_hosts: list[str] = []

    def multi_handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        hit_hosts.append(host)
        if "groq.com" in host:
            return httpx.Response(429, text="groq rate limit")
        if "mistral.ai" in host:
            raise httpx.ReadTimeout("mistral timeout", request=req)
        if "google" in host:
            return httpx.Response(429, text="google ip block")
        return httpx.Response(
            200,
            json={
                "responseStatus": 200,
                "detectedLanguage": "ru",
                "responseData": {"translatedText": "Resilient hello"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(multi_handler)) as client:
        translator = create_translator(
            chain_cfg.translator_provider,
            client,
            provider_specs=chain_cfg.translator_chain_specs,
            cooldown_seconds=chain_cfg.translator_cooldown,
            clock=clock,
        )
        assert isinstance(translator, FallbackTranslator)
        assert isinstance(translator.providers[0], OpenAITranslator)
        assert isinstance(translator.providers[1], OpenAITranslator)
        assert isinstance(translator.providers[2], GoogleTranslator)
        assert isinstance(translator.providers[3], MyMemoryTranslator)

        first = await translator.translate(TranslationRequest(text="Привет", target_lang="en"))
        assert first.translated_text == "Resilient hello"
        assert first.provider == "mymemory"
        assert hit_hosts[-1] == "api.mymemory.translated.net"

        # 2nd request 5s later goes STRAIGHT to mymemory in a single HTTP request!
        hit_hosts.clear()
        clock.advance(5.0)
        second = await translator.translate(TranslationRequest(text="Пока", target_lang="en"))
        assert second.provider == "mymemory"
        assert hit_hosts == ["api.mymemory.translated.net"]

    # 3. Validation errors for invalid chain / missing per-provider key
    with pytest.raises(ConfigError, match="TRANSLATOR_CHAIN"):
        Config.from_env(
            env={"BOT_TOKEN": "1:a", "TRANSLATOR_CHAIN": "groq,bad_provider"},
            dotenv_path=None,
        )
    with pytest.raises(ConfigError, match="MISTRAL_API_KEY"):
        Config.from_env(
            env={
                "BOT_TOKEN": "1:a",
                "TRANSLATOR_CHAIN": "groq,mistral",
                "GROQ_API_KEY": "gsk_ok",
            },
            dotenv_path=None,
        )

    # 4. SecretRedactingFilter scrubs all per-provider keys & MyMemory email
    configure_logging(
        "INFO",
        secrets_to_redact=(
            chain_cfg.bot_token,
            *(s.api_key for s in chain_cfg.translator_chain_specs),
            *(s.email for s in chain_cfg.translator_chain_specs),
        ),
    )
    with caplog.at_level(logging.WARNING):
        logging.getLogger("mivta.test").warning(
            "Leaked %s %s %s",
            "gsk_secret_1111",
            "mst_secret_2222",
            "priv_mail@example.com",
        )
    assert "gsk_secret_1111" not in caplog.text
    assert "mst_secret_2222" not in caplog.text
    assert "priv_mail@example.com" not in caplog.text


def test_translator_package_remains_independent_of_config_telegram_and_sqlite() -> None:
    code = (
        "import sys, mivta.translator\n"
        "forbidden = ('aiogram', 'aiosqlite', 'mivta.config', 'mivta.bot', 'mivta.database')\n"
        "loaded = [m for m in sys.modules if m == 'aiogram' or m.startswith(forbidden)]\n"
        "assert not loaded, f'Forbidden imports loaded: {loaded}'\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
