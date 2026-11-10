"""Google Translate JSON (gtx) HTTPX provider for zero-key operation."""

from __future__ import annotations

import httpx

from mivta.translator._http import send_and_parse_json, validate_request
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    Translator,
    TranslatorUnavailableError,
)
from mivta.utils.languages import normalize_detected_code

DEFAULT_GOOGLE_URL = "https://translate.googleapis.com"


class GoogleTranslator(Translator):
    """Keyless HTTPX translator using Google Translate's structured JSON endpoint."""

    name = "google"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = "",
        max_length: int = 500,
    ) -> None:
        self._client = client
        self._endpoint = f"{(base_url or DEFAULT_GOOGLE_URL).rstrip('/')}/translate_a/single"
        self._max_length = max_length

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        validate_request(request, self._max_length)
        if request.source_lang != "auto" and request.source_lang == request.target_lang:
            return TranslationResult(
                translated_text=request.text,
                source_lang=request.source_lang,
                target_lang=request.target_lang,
                style=request.style,
                provider=self.name,
            )

        params = {
            "client": "gtx",
            "sl": request.source_lang,
            "tl": request.target_lang,
            "dt": "t",
            "dj": "1",
            "q": request.text,
        }
        data = await send_and_parse_json(
            self._client,
            "GET",
            self._endpoint,
            provider=self.name,
            params=params,
        )

        if not isinstance(data, dict) or not isinstance(data.get("sentences"), list):
            raise TranslatorUnavailableError("google: malformed sentences payload")

        chunks: list[str] = []
        for sentence in data["sentences"]:
            if isinstance(sentence, dict) and isinstance(sentence.get("trans"), str):
                chunks.append(sentence["trans"])

        translated = "".join(chunks)
        if not translated.strip():
            raise TranslatorUnavailableError("google: received blank translation")

        raw_src = data.get("src")
        source_code = (
            normalize_detected_code(raw_src)
            if isinstance(raw_src, str) and raw_src
            else normalize_detected_code(request.source_lang)
        )
        return TranslationResult(
            translated_text=translated,
            source_lang=source_code,
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )
