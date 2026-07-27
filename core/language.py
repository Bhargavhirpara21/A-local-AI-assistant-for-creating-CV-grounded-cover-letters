"""Deterministic language detection for German and English job postings."""

from __future__ import annotations

import re
from typing import Final, Literal


_GERMAN_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "der",
        "die",
        "das",
        "und",
        "für",
        "mit",
        "wir",
        "sie",
        "ist",
        "im",
        "den",
        "bei",
        "eine",
        "als",
        "auf",
        "ihre",
        "sind",
        "werden",
        "aus",
        "dem",
        "nicht",
        "oder",
        "wird",
        "über",
        "zur",
        "zum",
    }
)

_ENGLISH_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "the",
        "and",
        "with",
        "for",
        "you",
        "are",
        "our",
        "we",
        "will",
        "of",
        "to",
        "in",
        "is",
        "on",
        "as",
        "be",
        "that",
        "have",
        "your",
        "from",
        "at",
        "or",
        "an",
        "by",
    }
)

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-zäöüß]+")


def detect_language(text: str) -> Literal["de", "en"]:
    """Detect German or English by counting language-specific stopword hits.

    Words are matched case-insensitively as complete tokens. German is the
    deterministic default when both languages have the same score or neither
    language has a stopword hit.

    Args:
        text: Job-description text to classify.

    Returns:
        ``"en"`` when English has more stopword hits; otherwise ``"de"``.
    """

    tokens = _TOKEN_PATTERN.findall(text.lower())
    german_hits = sum(token in _GERMAN_STOPWORDS for token in tokens)
    english_hits = sum(token in _ENGLISH_STOPWORDS for token in tokens)

    return "en" if english_hits > german_hits else "de"
