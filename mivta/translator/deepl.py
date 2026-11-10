"""DeepL REST API v2 translator using HTTPX."""

from __future__ import annotations

import re
from typing import Any

import httpx

from mivta.translator._http import send_and_parse_json, validate_request
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    TranslationStyle,
    Translator,
    TranslatorUnavailableError,
)
from mivta.utils.languages import normalize_detected_code

DEEPL_FREE_URL = "https://api-free.deepl.com/v2"
DEEPL_PRO_URL = "https://api.deepl.com/v2"

_DEEPL_FORMALITY: dict[TranslationStyle, str] = {
    TranslationStyle.NEUTRAL: "default",
    TranslationStyle.CASUAL: "prefer_less",
    TranslationStyle.FORMAL: "prefer_more",
}
_XML_STYLE_TAG_RE = re.compile(r"<s\d+>")


class DeepLTranslator(Translator):
    """Translator backed by DeepL API v2 with native formality support."""

    name = "deepl"
    supports_styles = True

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        base_url: str = "",
        max_length: int = 500,
    ) -> None:
        self._client = client
        self._api_key = api_key
        resolved_base = base_url or (
            DEEPL_FREE_URL if api_key.endswith(":fx") else DEEPL_PRO_URL
        )
        self._endpoint = f"{resolved_base.rstrip('/')}/translate"
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

        headers = {
            "Authorization": f"DeepL-Auth-Key {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "text": [request.text],
            "target_lang": request.target_lang.upper(),
            "preserve_formatting": True,
            "formality": _DEEPL_FORMALITY[request.style],
        }
        if _XML_STYLE_TAG_RE.search(request.text):
            payload["tag_handling"] = "xml"
        if request.source_lang != "auto":
            payload["source_lang"] = request.source_lang.upper()

        data = await send_and_parse_json(
            self._client,
            "POST",
            self._endpoint,
            provider=self.name,
            headers=headers,
            json_body=payload,
        )

        try:
            item = data["translations"][0]
            translated = item["text"]
            detected = item.get("detected_source_language")
        except (KeyError, IndexError, TypeError) as exc:
            raise TranslatorUnavailableError("deepl: malformed translations payload") from exc

        if not isinstance(translated, str) or not translated.strip():
            raise TranslatorUnavailableError("deepl: received blank translation")

        source_code = (
            normalize_detected_code(detected)
            if isinstance(detected, str) and detected
            else normalize_detected_code(request.source_lang)
        )
        return TranslationResult(
            translated_text=translated,
            source_lang=source_code,
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )
