"""Tests for HTTPX-based translator providers and error handling."""

from __future__ import annotations

import json

import httpx
import pytest

from mivta.translator import (
    DeepLTranslator,
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
