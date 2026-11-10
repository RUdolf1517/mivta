"""Internal HTTP error mapping and request validation helpers for translators."""

from __future__ import annotations

import json
from typing import Any

import httpx

from mivta.translator.base import (
    InvalidTranslationRequestError,
    TextTooLongError,
    TranslationRequest,
    TranslatorAuthError,
    TranslatorRateLimitError,
    TranslatorTimeoutError,
    TranslatorUnavailableError,
)

MAX_UPSTREAM_RESPONSE_BYTES = 1_048_576  # 1 MiB cap against oversized upstream payloads


def validate_request(request: TranslationRequest, max_length: int) -> None:
    """Validate common constraints before issuing any network call."""
    if not request.text or not request.text.strip():
        raise InvalidTranslationRequestError("Input text must not be empty")
    if len(request.text) > max_length:
        raise TextTooLongError(
            f"Input text length ({len(request.text)}) exceeds limit ({max_length})"
        )
    if not request.target_lang or request.target_lang == "auto":
        raise InvalidTranslationRequestError("Target language must be an explicit language code")
    if not request.source_lang:
        raise InvalidTranslationRequestError("Source language must not be empty")


async def send_and_parse_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    provider: str,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
    max_response_bytes: int = MAX_UPSTREAM_RESPONSE_BYTES,
) -> Any:
    """Execute an async HTTP request with bounded body size and map failures to TranslatorError."""
    try:
        async with client.stream(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
        ) as response:
            status = response.status_code
            if status in (401, 403):
                raise TranslatorAuthError(
                    f"{provider}: authentication rejected (HTTP {status})"
                )
            if status in (429, 456):
                raise TranslatorRateLimitError(
                    f"{provider}: rate limit or quota exceeded (HTTP {status})"
                )
            if status == 400:
                raise InvalidTranslationRequestError(
                    f"{provider}: bad translation request (HTTP 400)"
                )
            if status >= 500 or status != 200:
                raise TranslatorUnavailableError(
                    f"{provider}: upstream returned HTTP {status}"
                )

            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_response_bytes:
                    raise TranslatorUnavailableError(
                        f"{provider}: response exceeded {max_response_bytes} bytes limit"
                    )
                chunks.append(chunk)
            raw_body = b"".join(chunks)
    except httpx.TimeoutException as exc:
        raise TranslatorTimeoutError(f"{provider}: request timed out") from exc
    except httpx.HTTPError as exc:
        err_type = type(exc).__name__
        raise TranslatorUnavailableError(f"{provider}: network error ({err_type})") from exc

    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TranslatorUnavailableError(f"{provider}: invalid JSON response") from exc
