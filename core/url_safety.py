"""Shared fail-closed validation for externally supplied HTTP(S) URLs."""

from __future__ import annotations

import unicodedata
from urllib.parse import urlparse

_UNSAFE_URL_CHARACTERS = frozenset('<>"{}|\\^`')


def validate_http_url(value: str) -> str | None:
    """Return an unchanged clean absolute HTTP(S) URL, otherwise ``None``."""

    if not value or value != value.strip():
        return None
    if any(
        character.isspace()
        or unicodedata.category(character).startswith("C")
        or character in _UNSAFE_URL_CHARACTERS
        for character in value
    ):
        return None
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        _validated_port = parsed.port
    except ValueError:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.scheme.casefold() not in ("http", "https"):
        return None
    if not parsed.netloc or not hostname:
        return None
    return value
