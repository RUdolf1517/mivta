"""Deep tests for FallbackTranslator, factory presets, malformed upstream payloads, and LLM JSON."""

from __future__ import annotations

import json
from typing import Any

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
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorError,
    TranslatorRateLimitError,
    TranslatorUnavailableError,
    create_http_client,
    create_translator,
)


class ScriptedProvider(Translator):
    def __init__(
        self,
        name: str,
        *,
        supports_styles: bool = False,
        uses_llm_glossary: bool = False,
        fail_with: TranslatorError | None = None,
    ) -> None:
        self.name = name
        self.supports_styles = supports_styles
        self.uses_llm_glossary = uses_llm_glossary
        self.fail_with = fail_with
        self.calls: int = 0
        self.style_calls: int = 0
        self.closed: bool = False

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return TranslationResult(
            translated_text=f"{self.name}:{request.text}",
            source_lang=request.source_lang,
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
        if self.fail_with is not None:
            raise self.fail_with
        return [
            TranslationResult(
                translated_text=f"{self.name}:{st.value}:{request.text}",
                source_lang=request.source_lang,
                target_lang=request.target_lang,
                style=st,
                provider=self.name,
            )
            for st in styles
        ]

    async def aclose(self) -> None:
        self.closed = True


async def test_fallback_translator_chain_multistyle_glossary_flags_and_close() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        FallbackTranslator([])

    # uses_llm_glossary is True ONLY if all providers in chain support LLM glossary
    p_llm1 = ScriptedProvider(
        "openai",
        supports_styles=True,
        uses_llm_glossary=True,
        fail_with=TranslatorRateLimitError("429"),
    )
    p_llm2 = ScriptedProvider(
        "groq",
        supports_styles=True,
        uses_llm_glossary=True,
        fail_with=TranslatorUnavailableError("503"),
    )
    p_google = ScriptedProvider("google", supports_styles=False, uses_llm_glossary=False)

    all_llm = FallbackTranslator([p_llm1, p_llm2])
    assert all_llm.uses_llm_glossary is True
    assert all_llm.providers == (p_llm1, p_llm2)
    # When all providers fail, last exception is raised
    with pytest.raises(TranslatorUnavailableError, match="503"):
        await all_llm.translate(TranslationRequest(text="Hello", target_lang="ru"))

    chain = FallbackTranslator([p_llm1, p_llm2, p_google])
    assert chain.name == "openai->groq->google"
    assert chain.supports_styles is True
    assert chain.uses_llm_glossary is False

    # 3-step recovery for single translate AND multi-style translate_styles
    res = await chain.translate(TranslationRequest(text="Hello", target_lang="ru"))
    assert res.translated_text == "google:Hello"
    assert res.provider == "google"

    styles = [TranslationStyle.NEUTRAL, TranslationStyle.FORMAL]
    st_res = await chain.translate_styles(
        TranslationRequest(text="Hello", target_lang="ru"),
        styles,
    )
    assert [r.translated_text for r in st_res] == ["google:neutral:Hello", "google:formal:Hello"]

    # Local validation errors (TextTooLongError, blank text, target="auto", empty source)
    # MUST fail fast on provider #1 without calling fallbacks!
    p_google.calls = 0
    for local_exc, bad_req in [
        (TextTooLongError("too long"), TranslationRequest(text="x", target_lang="ru")),
        (InvalidTranslationRequestError("blank"), TranslationRequest(text="  ", target_lang="ru")),
        (
            InvalidTranslationRequestError("auto tgt"),
            TranslationRequest(text="hi", target_lang="auto"),
        ),
        (
            InvalidTranslationRequestError("no src"),
            TranslationRequest(text="hi", source_lang="", target_lang="ru"),
        ),
    ]:
        p_first = ScriptedProvider("first", fail_with=local_exc)
        fb = FallbackTranslator([p_first, p_google])
        with pytest.raises(type(local_exc)):
            await fb.translate(bad_req)
        with pytest.raises(type(local_exc)):
            await fb.translate_styles(bad_req, styles)
    assert p_google.calls == 0

    await chain.aclose()
    assert p_llm1.closed and p_llm2.closed and p_google.closed


async def test_factory_presets_deduplication_http_client_and_unknown_provider() -> None:
    client = create_http_client(timeout_seconds=2.5)
    try:
        assert client.headers["User-Agent"].startswith("mivta/")
        assert client.follow_redirects is False

        with pytest.raises(TranslatorError, match="Unknown translator provider"):
            create_translator("nonexistent_engine", client)

        # Duplicate fallback matching primary unwraps cleanly to single provider
        single = create_translator(
            "google",
            client,
            fallback_providers=("google", "  "),
        )
        assert isinstance(single, GoogleTranslator)

        # Multi-provider chain skips duplicates while preserving order
        multi = create_translator(
            "groq",
            client,
            api_key="gsk_test",
            fallback_providers=("groq", "libretranslate", "google"),
        )
        assert isinstance(multi, FallbackTranslator)
        assert multi.name == "groq->libretranslate->google"

        deepl = create_translator("deepl", client, api_key="key:fx")
        assert isinstance(deepl, DeepLTranslator)
    finally:
        await client.aclose()


async def test_openai_multistyle_swap_fenced_json_fallback_and_adversarial_outputs() -> None:
    responses: list[dict[str, Any]] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        llm = OpenAITranslator(
            client,
            api_key="sk-test",
            glossary=("mivta",),
        )

        # 1. Manual source == target in translate_styles skips network completely
        same_styles = await llm.translate_styles(
            TranslationRequest(text="Same", source_lang="en", target_lang="en"),
            [TranslationStyle.NEUTRAL, TranslationStyle.CASUAL],
        )
        assert len(same_styles) == 1 and same_styles[0].translated_text == "Same"

        # 2. 1-call multistyle + swap_lang in fenced ```json ... ``` AND clamping rogue target
        fenced_swap = (
            "```json\n"
            + json.dumps(
                {
                    "source_lang": "en-US",
                    "target_lang": "fr",  # Rogue target not in {en, pl} -> clamped to 'en'!
                    "neutral": "<source_text_12ab34cd>Dzień dobry</source_text_12ab34cd>",
                    "casual": "Cześć",
                }
            )
            + "\n```"
        )
        responses.append({"choices": [{"message": {"content": fenced_swap}}]})
        res_fenced = await llm.translate_styles(
            TranslationRequest(
                text="Good morning MIVTAK0X <s0>friend</s0> source_text_00000000",
                source_lang="auto",
                target_lang="en",
                swap_lang="pl",
            ),
            [TranslationStyle.NEUTRAL, TranslationStyle.CASUAL],
        )
        assert [r.translated_text for r in res_fenced] == ["Dzień dobry", "Cześć"]
        assert all(r.target_lang == "en" and r.source_lang == "en" for r in res_fenced)

        # 3. Local model ignores JSON format on translate_styles -> falls back to per-style calls!
        responses.extend(
            [
                {"choices": [{"message": {"content": "Not valid JSON at all"}}]},
                {"choices": [{"message": {"content": "Neutral text"}}]},
                {"choices": [{"message": {"content": "Formal text"}}]},
            ]
        )
        fb_styles = await llm.translate_styles(
            TranslationRequest(text="Привет", source_lang="ru", target_lang="en"),
            [TranslationStyle.NEUTRAL, TranslationStyle.FORMAL],
        )
        assert [r.translated_text for r in fb_styles] == ["Neutral text", "Formal text"]

        # 4. Single-style call simplifies translate_styles([FORMAL]) to 1 translate() call
        responses.append({"choices": [{"message": {"content": "Good day"}}]})
        one_style = await llm.translate_styles(
            TranslationRequest(text="Добрый день", source_lang="ru", target_lang="en"),
            [TranslationStyle.FORMAL],
        )
        assert len(one_style) == 1 and one_style[0].style == TranslationStyle.FORMAL

        # 5. Malformed / empty choices & blank content raise TranslatorUnavailableError
        for bad_payload in (
            {},
            {"choices": []},
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"message": {"content": "   \n  "}}]},
        ):
            responses.append(bad_payload)
            with pytest.raises(TranslatorUnavailableError):
                await llm.translate(TranslationRequest(text="Привет", target_lang="en"))


async def test_deepl_google_libre_and_http_transport_error_edge_cases() -> None:
    # 1. Network ConnectError and HTML non-JSON 200 response
    def connect_err_handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=req)

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect_err_handler)) as c1:
        with pytest.raises(TranslatorUnavailableError, match="network error"):
            await GoogleTranslator(c1).translate(
                TranslationRequest(text="Привет", target_lang="en")
            )

    def html_200_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>Gateway HTML</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(html_200_handler)) as c2:
        with pytest.raises(TranslatorUnavailableError, match="invalid JSON"):
            await GoogleTranslator(c2).translate(
                TranslationRequest(text="Привет", target_lang="en")
            )

    # 2. Malformed JSON structures for Google, DeepL (pro key URL), and LibreTranslate
    def empty_json_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(empty_json_handler)) as c3:
        with pytest.raises(TranslatorUnavailableError):
            await GoogleTranslator(c3).translate(
                TranslationRequest(text="Привет", target_lang="en")
            )
        with pytest.raises(TranslatorUnavailableError):
            await DeepLTranslator(c3, api_key="pro-key-no-fx").translate(
                TranslationRequest(text="Привет", target_lang="en")
            )
        with pytest.raises(TranslatorUnavailableError):
            await LibreTranslateTranslator(c3, api_key="lt-key").translate(
                TranslationRequest(text="Привет", target_lang="en")
            )
