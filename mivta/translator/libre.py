"""LibreTranslate HTTP translator using HTTPX."""

from __future__ import annotations

from typing import Any

import httpx

from mivta.translator._http import send_and_parse_json, validate_request
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    Translator,
    TranslatorUnavailableError,
)
from mivta.utils.languages import normalize_detected_code

DEFAULT_LIBRE_URL = "http://localhost:5000"


class LibreTranslateTranslator(Translator):
    """Translator backed by a self-hosted or hosted LibreTranslate server."""

    name = "libretranslate"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = "",
        api_key: str = "",
        max_length: int = 500,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._endpoint = f"{(base_url or DEFAULT_LIBRE_URL).rstrip('/')}/translate"
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

        payload: dict[str, Any] = {
            "q": request.text,
            "source": request.source_lang,
            "target": request.target_lang,
            "format": "text",
        }
        if self._api_key:
            payload["api_key"] = self._api_key

        data = await send_and_parse_json(
            self._client,
            "POST",
            self._endpoint,
            provider=self.name,
            headers={"Content-Type": "application/json"},
            json_body=payload,
        )

        if not isinstance(data, dict) or not isinstance(data.get("translatedText"), str):
            raise TranslatorUnavailableError("libretranslate: malformed response payload")

        translated = data["translatedText"]
        if not translated.strip():
            raise TranslatorUnavailableError("libretranslate: received blank translation")

        detected_obj = data.get("detectedLanguage")
        detected_lang = (
            detected_obj.get("language") if isinstance(detected_obj, dict) else None
        )
        source_code = (
            normalize_detected_code(detected_lang)
            if isinstance(detected_lang, str) and detected_lang
            else normalize_detected_code(request.source_lang)
        )
        return TranslationResult(
            translated_text=translated,
            source_lang=source_code,
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )
