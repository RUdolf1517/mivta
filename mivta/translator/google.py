"""Google Translate JSON HTTPX provider with multi-endpoint VPS 429 failover."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from mivta.translator._http import send_and_parse_json, validate_request
from mivta.translator.base import (
    InvalidTranslationRequestError,
    TranslationRequest,
    TranslationResult,
    Translator,
    TranslatorError,
    TranslatorUnavailableError,
)
from mivta.utils.languages import normalize_detected_code

DEFAULT_GOOGLE_URL = "https://translate.googleapis.com"
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class _GoogleEndpoint:
    url: str
    extra_params: tuple[tuple[str, str], ...]


_DEFAULT_ENDPOINTS: tuple[_GoogleEndpoint, ...] = (
    _GoogleEndpoint(
        url="https://translate.googleapis.com/translate_a/single",
        extra_params=(("client", "gtx"), ("dt", "t"), ("dj", "1")),
    ),
    _GoogleEndpoint(
        url="https://clients5.google.com/translate_a/t",
        extra_params=(("client", "dict-chrome-ex"),),
    ),
    _GoogleEndpoint(
        url="https://translate.google.com/translate_a/single",
        extra_params=(("client", "at"), ("dt", "t"), ("dj", "1")),
    ),
)


def _extract_google_payload(data: Any, fallback_source: str) -> tuple[str, str]:
    """Parse structured `dj=1` dicts and `clients5` array payloads (`[[text, src]]` / `[text]`)."""
    if isinstance(data, dict) and isinstance(data.get("sentences"), list):
        chunks = [
            s["trans"]
            for s in data["sentences"]
            if isinstance(s, dict) and isinstance(s.get("trans"), str)
        ]
        translated = "".join(chunks)
        raw_src = data.get("src")
        src = (
            normalize_detected_code(raw_src)
            if isinstance(raw_src, str) and raw_src
            else normalize_detected_code(fallback_source)
        )
        return translated, src

    if isinstance(data, list) and data:
        # `clients5` with sl=<explicit>: ["Translated text"]
        if all(isinstance(item, str) for item in data):
            return "".join(data), normalize_detected_code(fallback_source)

        # `clients5` with sl=auto: [["Translated text", "ru"]]
        first = data[0]
        if (
            isinstance(first, list)
            and first
            and isinstance(first[0], str)
        ):
            chunks_pair: list[str] = []
            detected = ""
            for entry in data:
                if isinstance(entry, list) and entry and isinstance(entry[0], str):
                    chunks_pair.append(entry[0])
                    if not detected and len(entry) >= 2 and isinstance(entry[1], str):
                        detected = entry[1]
            src = (
                normalize_detected_code(detected)
                if detected
                else normalize_detected_code(fallback_source)
            )
            return "".join(chunks_pair), src

        # Classic `translate_a/single` without `dj=1`: [[["trans", "orig", ...], ...], null, "ru"]
        if isinstance(first, list) and first and isinstance(first[0], list):
            seg_chunks = [
                seg[0]
                for seg in first
                if isinstance(seg, list) and seg and isinstance(seg[0], str)
            ]
            raw_s = data[2] if len(data) > 2 and isinstance(data[2], str) else ""
            src = (
                normalize_detected_code(raw_s)
                if raw_s
                else normalize_detected_code(fallback_source)
            )
            return "".join(seg_chunks), src

    raise TranslatorUnavailableError("google: malformed translation payload")


class GoogleTranslator(Translator):
    """Keyless HTTPX translator rotating official Google endpoints on datacenter 429 blocks."""

    name = "google"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str = "",
        max_length: int = 500,
    ) -> None:
        self._client = client
        self._max_length = max_length
        if base_url:
            clean = base_url.rstrip("/")
            url = clean if "/translate_a/" in clean else f"{clean}/translate_a/single"
            params = (
                (("client", "dict-chrome-ex"),)
                if clean.endswith("/translate_a/t")
                else (("client", "gtx"), ("dt", "t"), ("dj", "1"))
            )
            self._endpoints: tuple[_GoogleEndpoint, ...] = (
                _GoogleEndpoint(url=url, extra_params=params),
            )
        else:
            self._endpoints = _DEFAULT_ENDPOINTS
        self._preferred_idx = 0

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

        total = len(self._endpoints)
        start = self._preferred_idx
        headers = {"User-Agent": _BROWSER_USER_AGENT}
        last_exc: TranslatorError | None = None

        for offset in range(total):
            idx = (start + offset) % total
            ep = self._endpoints[idx]
            params = dict(ep.extra_params)
            params["sl"] = request.source_lang
            params["tl"] = request.target_lang
            params["q"] = request.text

            try:
                data = await send_and_parse_json(
                    self._client,
                    "GET",
                    ep.url,
                    provider=self.name,
                    headers=headers,
                    params=params,
                )
                translated, source_code = _extract_google_payload(data, request.source_lang)
                if not translated.strip():
                    raise TranslatorUnavailableError("google: received blank translation")
            except InvalidTranslationRequestError:
                raise
            except TranslatorError as exc:
                last_exc = exc
                continue

            self._preferred_idx = idx
            return TranslationResult(
                translated_text=translated,
                source_lang=source_code,
                target_lang=request.target_lang,
                style=request.style,
                provider=self.name,
            )

        assert last_exc is not None
        raise last_exc
