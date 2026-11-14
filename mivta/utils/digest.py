"""Stable, collision-resistant digest keys for cached translations.

The cache and the singleflight inflight map both key on the same tuple
(provider, source, target, swap, style, text), so the hashing lives here once.
Keys are 16-byte BLAKE2b digests over length-prefixed fields: user text never
sits in memory in plaintext, and length prefixing keeps `("ab", "c")` and
`("a", "bc")` from colliding.
"""

from __future__ import annotations

import hashlib

from mivta.translator.base import TranslationRequest


def translation_digest(provider: str, request: TranslationRequest) -> bytes:
    """Create a collision-free compact 16-byte key so plaintext inputs aren't duplicated."""
    hasher = hashlib.blake2b(digest_size=16)
    for part in (
        provider,
        request.source_lang,
        request.target_lang,
        request.swap_lang,
        request.style.value,
        request.text,
    ):
        encoded = part.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
    return hasher.digest()
