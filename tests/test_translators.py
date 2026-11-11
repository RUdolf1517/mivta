"""Tests for HTTPX-based translator providers and error handling."""

from __future__ import annotations

import json

import httpx
import pytest

from mivta.translator import (
    DeepLTranslator,
    FallbackTranslator,
    GoogleTranslator,
    InvalidTranslationRequestError,
    LibreTranslateTranslator,
    OpenAITranslator,
    TextTooLongError,
    TranslationRequest,
    TranslationStyle,
    TranslatorAuthError,
    TranslatorRateLimitError,
    TranslatorTimeoutError,
    TranslatorUnavailableError,
    create_translator,
)


async def test_openai_translator_styles_and_multiline() -> None:
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer sk-test"
        captured_payload.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Guten Tag!\nWie geht es Ihnen?",
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = OpenAITranslator(client, api_key="sk-test", model="gpt-4o-mini")
        result = await translator.translate(
            TranslationRequest(
                text="Добрый день!\nКак у вас дела?",
                source_lang="ru",
                target_lang="de",
                style=TranslationStyle.FORMAL,
            )
        )

    assert result.translated_text == "Guten Tag!\nWie geht es Ihnen?"
    assert result.source_lang == "ru"
    assert result.target_lang == "de"
    assert result.style == TranslationStyle.FORMAL
    system_prompt = captured_payload["messages"][0]["content"]
    assert "formal" in system_prompt.lower()
    assert "Russian (ru)" in system_prompt


async def test_deepl_translator_free_endpoint_and_formality() -> None:
    seen_url = ""
    seen_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_url
        seen_url = str(request.url)
        assert request.headers["Authorization"] == "DeepL-Auth-Key test-key:fx"
        seen_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "translations": [
                    {
                        "detected_source_language": "RU",
                        "text": "Hey, what's up?",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = DeepLTranslator(client, api_key="test-key:fx")
        result = await translator.translate(
            TranslationRequest(
                text="Привет, чё как?",
                source_lang="auto",
                target_lang="en",
                style=TranslationStyle.CASUAL,
            )
        )

    assert seen_url == "https://api-free.deepl.com/v2/translate"
    assert seen_body["formality"] == "prefer_less"
    assert seen_body["preserve_formatting"] is True
    assert "source_lang" not in seen_body
    assert result.translated_text == "Hey, what's up?"
    assert result.source_lang == "ru"


async def test_libretranslate_provider() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["q"] == "שלום עולם"
        assert body["source"] == "auto"
        assert body["target"] == "en"
        return httpx.Response(
            200,
            json={
                "translatedText": "Hello world",
                "detectedLanguage": {"confidence": 99.0, "language": "he"},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = LibreTranslateTranslator(client, base_url="http://libre.local")
        result = await translator.translate(
            TranslationRequest(text="שלום עולם", target_lang="en")
        )

    assert result.translated_text == "Hello world"
    assert result.source_lang == "he"


async def test_google_translator_multi_sentence_and_legacy_code_normalization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["sl"] == "auto"
        assert request.url.params["tl"] == "en"
        assert request.url.params["dj"] == "1"
        return httpx.Response(
            200,
            json={
                "sentences": [
                    {"trans": "Hello! ", "orig": "שלום! "},
                    {"trans": "How are you?", "orig": "מה שלומך?"},
                ],
                "src": "iw",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = GoogleTranslator(client)
        result = await translator.translate(
            TranslationRequest(text="שלום! מה שלומך?", target_lang="en")
        )

    assert result.translated_text == "Hello! How are you?"
    assert result.source_lang == "he"


async def test_same_manual_source_and_target_skips_network() -> None:
    def fail_if_called(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Network should not be called when manual source == target")

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail_if_called)) as client:
        translator = DeepLTranslator(client, api_key="key:fx")
        res = await translator.translate(
            TranslationRequest(text="Same language", source_lang="en", target_lang="en")
        )
        assert res.translated_text == "Same language"


@pytest.mark.parametrize(
    ("status_code", "expected_exc"),
    [
        (401, TranslatorAuthError),
        (403, TranslatorAuthError),
        (429, TranslatorRateLimitError),
        (456, TranslatorRateLimitError),
        (400, InvalidTranslationRequestError),
        (502, TranslatorUnavailableError),
    ],
)
async def test_http_status_error_mapping(
    status_code: int,
    expected_exc: type[Exception],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = GoogleTranslator(client)
        with pytest.raises(expected_exc):
            await translator.translate(TranslationRequest(text="Привет", target_lang="en"))


async def test_timeout_and_length_validation() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        translator = GoogleTranslator(client, max_length=10)
        with pytest.raises(TextTooLongError):
            await translator.translate(
                TranslationRequest(text="01234567890", target_lang="en")
            )
        with pytest.raises(TranslatorTimeoutError):
            await translator.translate(TranslationRequest(text="Привет", target_lang="en"))


async def test_llm_presets_glossary_and_single_call_multistyle_batch() -> None:
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        calls.append((str(request.url), body))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                "```json\n"
                                '{"neutral": "Deploy mivta now",'
                                ' "casual": "Ship mivta now",'
                                ' "formal": "Please deploy mivta now"}\n'
                                "```"
                            )
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = create_translator(
            "groq",
            client,
            api_key="gsk_test",
            glossary=("mivta", "Kubernetes"),
        )
        assert translator.name == "groq"
        assert translator.supports_styles is True
        assert translator.uses_llm_glossary is True

        results = await translator.translate_styles(
            TranslationRequest(text="Задеплой mivta сейчас", target_lang="en"),
            [TranslationStyle.NEUTRAL, TranslationStyle.CASUAL, TranslationStyle.FORMAL],
        )

    # All 3 style registers resolved in a SINGLE HTTP request to Groq's endpoint!
    assert len(calls) == 1
    url, payload = calls[0]
    assert url == "https://api.groq.com/openai/v1/chat/completions"
    assert payload["model"] == "llama-3.3-70b-versatile"
    assert "mivta, Kubernetes" in payload["messages"][0]["content"]
    assert [r.style for r in results] == [
        TranslationStyle.NEUTRAL,
        TranslationStyle.CASUAL,
        TranslationStyle.FORMAL,
    ]
    assert [r.translated_text for r in results] == [
        "Deploy mivta now",
        "Ship mivta now",
        "Please deploy mivta now",
    ]


async def test_deepl_enables_xml_tag_handling_when_style_tags_present() -> None:
    seen_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "translations": [
                    {"detected_source_language": "RU", "text": "<s0>Important</s0>"}
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        translator = DeepLTranslator(client, api_key="key:fx")
        res = await translator.translate(
            TranslationRequest(text="<s0>Важно</s0>", target_lang="en")
        )
    assert seen_body["tag_handling"] == "xml"
    assert res.translated_text == "<s0>Important</s0>"


async def test_fallback_translator_recovers_on_429_and_400_but_not_local_validation() -> None:
    hit_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        hit_urls.append(url)
        if "groq.com" in url:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(
            200,
            json={"sentences": [{"trans": "Fallback translation"}], "src": "ru"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        chain = create_translator(
            "groq",
            client,
            api_key="gsk_key",
            max_length=20,
            fallback_providers=("google",),
        )
        assert isinstance(chain, FallbackTranslator)
        assert chain.name == "groq->google"
        assert chain.uses_llm_glossary is False

        # 1. Local TextTooLongError rejects immediately with zero network calls
        with pytest.raises(TextTooLongError):
            await chain.translate(TranslationRequest(text="X" * 25, target_lang="en"))
        assert len(hit_urls) == 0

        # 2. Primary 429 transparently falls back to Google
        result = await chain.translate(TranslationRequest(text="Привет", target_lang="en"))
        assert result.translated_text == "Fallback translation"
        assert result.provider == "google"
        assert len(hit_urls) == 2


async def test_google_translator_429_failover_to_clients5_with_sticky_endpoint() -> None:
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        seen_hosts.append(host)
        assert "Mozilla/5.0" in request.headers.get("User-Agent", "")
        if host == "translate.googleapis.com":
            return httpx.Response(429, text="unusual traffic")
        if host == "clients5.google.com":
            if request.url.params["sl"] == "auto":
                return httpx.Response(200, json=[["Hello world", "ru"]])
            return httpx.Response(200, json=["Hello explicit"])
        return httpx.Response(502, text="bad gateway")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gt = GoogleTranslator(client)
        r1 = await gt.translate(TranslationRequest(text="Привет мир", target_lang="en"))
        assert r1.translated_text == "Hello world"
        assert r1.source_lang == "ru"
        assert seen_hosts == ["translate.googleapis.com", "clients5.google.com"]

        # Second call goes straight to sticky `clients5.google.com` without hitting 429 again!
        r2 = await gt.translate(
            TranslationRequest(text="Привет", source_lang="ru", target_lang="en")
        )
        assert r2.translated_text == "Hello explicit"
        assert seen_hosts == [
            "translate.googleapis.com",
            "clients5.google.com",
            "clients5.google.com",
        ]

