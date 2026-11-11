"""Keyless MyMemory REST API translator using HTTPX."""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

from mivta.translator._http import send_and_parse_json, validate_request
from mivta.translator.base import (
    TranslationRequest,
    TranslationResult,
    Translator,
    TranslatorAuthError,
    TranslatorRateLimitError,
    TranslatorUnavailableError,
    UnsupportedLanguageError,
)
from mivta.utils.languages import detect_single_script, normalize_detected_code

DEFAULT_MYMEMORY_URL = "https://api.mymemory.translated.net"
_MARKUP_OR_TOKEN_RE = re.compile(r"</?s\d+>|MIVTA[VK]\d+X")

# Scripts with a single dominant ISO-639-1 language in our registry
_UNIQUE_SCRIPT_LANG: dict[str, str] = {
    "hebrew": "he",
    "hangul": "ko",
    "greek": "el",
    "georgian": "ka",
    "armenian": "hy",
    "devanagari": "hi",
    "thai": "th",
}


def _restore_html_entities(original: str, translated: str) -> str:
    """Unescape MyMemory HTML entities (`&#39;`, `&quot;`, `&lt;s0&gt;`) when safe."""
    if "&" in translated and "&" not in original:
        return html.unescape(translated)
    return translated


def _parse_status_code(raw: Any) -> int:
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.isdigit():
            return int(stripped)
    return 0


class MyMemoryTranslator(Translator):
    """Free REST translator backed by MyMemory (`api.mymemory.translated.net`).

    Works out of the box with zero keys (5,000 chars/day per IP) or up to
    50,000 chars/day free when a contact email (`de=`) is supplied.
    """

    name = "mymemory"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = "",
        api_key: str = "",
        email: str = "",
        max_length: int = 500,
    ) -> None:
        self._client = client
        self._endpoint = f"{(base_url or DEFAULT_MYMEMORY_URL).rstrip('/')}/get"
        if email:
            self._email = email.strip()
            self._api_key = api_key.strip() if "@" not in api_key else ""
        elif "@" in api_key:
            self._email = api_key.strip()
            self._api_key = ""
        else:
            self._email = ""
            self._api_key = api_key.strip()
        self._max_length = max_length

    def _resolve_source_param(self, request: TranslationRequest) -> str:
        if request.source_lang != "auto":
            return request.source_lang
        probe = _MARKUP_OR_TOKEN_RE.sub("", request.text)
        script = detect_single_script(probe)
        if script is not None:
            inferred = _UNIQUE_SCRIPT_LANG.get(script)
            if inferred is not None:
                return inferred
        return "autodetect"

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        validate_request(request, self._max_length)
        src_param = self._resolve_source_param(request)
        if src_param != "autodetect" and src_param == request.target_lang:
            return TranslationResult(
                translated_text=request.text,
                source_lang=src_param,
                target_lang=request.target_lang,
                style=request.style,
                provider=self.name,
            )

        params: dict[str, str] = {
            "q": request.text,
            "langpair": f"{src_param}|{request.target_lang}",
        }
        if self._email:
            params["de"] = self._email
        if self._api_key:
            params["key"] = self._api_key

        data = await send_and_parse_json(
            self._client,
            "GET",
            self._endpoint,
            provider=self.name,
            params=params,
        )
        if not isinstance(data, dict):
            raise TranslatorUnavailableError("mymemory: malformed response payload")

        status = _parse_status_code(data.get("responseStatus"))
        details = str(data.get("responseDetails") or "")
        details_upper = details.upper()

        resp_data = data.get("responseData")
        raw_translated = (
            resp_data.get("translatedText") if isinstance(resp_data, dict) else None
        )
        translated_upper = raw_translated.upper() if isinstance(raw_translated, str) else ""

        if (
            status == 429
            or data.get("quotaFinished") is True
            or "MYMEMORY WARNING" in details_upper
            or "MYMEMORY WARNING" in translated_upper
            or "YOU USED ALL AVAILABLE FREE TRANSLATIONS" in details_upper
            or "YOU USED ALL AVAILABLE FREE TRANSLATIONS" in translated_upper
        ):
            raise TranslatorRateLimitError("mymemory: free translation quota exceeded")

        if "PLEASE SELECT TWO DISTINCT LANGUAGES" in details_upper or (
            "PLEASE SELECT TWO DISTINCT LANGUAGES" in translated_upper
        ):
            return TranslationResult(
                translated_text=request.text,
                source_lang=request.target_lang,
                target_lang=request.target_lang,
                style=request.style,
                provider=self.name,
            )

        if "INVALID LANGUAGE PAIR" in details_upper or "IS AN INVALID" in details_upper:
            raise UnsupportedLanguageError(
                f"mymemory: unsupported language pair ({src_param}->{request.target_lang})"
            )

        if status in (401, 403):
            raise TranslatorAuthError(
                f"mymemory: credentials rejected ({details or status})"
            )
        if status != 200:
            raise TranslatorUnavailableError(
                f"mymemory: upstream returned status {status or 'unknown'}"
            )

        if not isinstance(raw_translated, str) or not raw_translated.strip():
            raise TranslatorUnavailableError("mymemory: received blank translation")

        detected_raw = (
            resp_data.get("detectedLanguage")
            if isinstance(resp_data, dict)
            else None
        ) or data.get("detectedLanguage")
        if isinstance(detected_raw, str) and detected_raw.strip():
            detected_source = normalize_detected_code(detected_raw)
        elif src_param != "autodetect":
            detected_source = normalize_detected_code(src_param)
        else:
            detected_source = "auto"

        cleaned = _restore_html_entities(request.text, raw_translated)
        return TranslationResult(
            translated_text=cleaned,
            source_lang=detected_source,
            target_lang=request.target_lang,
            style=request.style,
            provider=self.name,
        )
