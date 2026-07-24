"""Backend-neutral language-model contracts for AutoCover."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LLMResult:
    """Normalized text result returned by any supported model backend."""

    text: str
    is_error: bool = False
    error_message: str | None = None
    cost_usd: float | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Actionable backend health state suitable for display in the UI."""

    ok: bool
    detail: str


@runtime_checkable
class LLMClient(Protocol):
    """Operations required from a swappable AutoCover model backend."""

    def generate(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        """Generate tool-free text using the supplied system and user prompts."""

        ...

    def import_cv(self, pdf_path: Path) -> LLMResult:
        """Convert one local CV PDF into reviewable Markdown text."""

        ...

    def research_job(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        """Research official vacancy sources with a backend-bounded tool set."""

        ...

    def health_check(self) -> HealthStatus:
        """Check backend availability and return actionable failure guidance."""

        ...
