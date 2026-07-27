"""Immutable application configuration and local path construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration passed explicitly to AutoCover components."""

    project_root: Path
    prompts_dir: Path
    style_examples_dir: Path
    data_dir: Path
    source_library_dir: Path
    uploads_dir: Path
    cache_dir: Path
    letters_dir: Path
    cv_dir: Path
    cv_versions_dir: Path
    cv_staging_dir: Path
    cv_active_path: Path
    cv_pending_path: Path
    cv_pending_recovery_path: Path
    applications_path: Path
    system_prompt_cache_path: Path
    backend: str = "agent_sdk"
    sdk_model: str | None = None
    grounding_model: str | None = "haiku"
    research_model: str | None = "haiku"
    api_model: str = "claude-opus-4-8"
    cli_path: Path | None = None
    grounding_enabled_default: bool = True
    generation_max_turns: int = 2
    import_max_turns: int = 8
    research_max_turns: int = 6
    max_source_zip_bytes: int = 5 * 1024 * 1024
    max_source_entries: int = 20
    max_source_file_bytes: int = 1024 * 1024
    max_source_total_bytes: int = 5 * 1024 * 1024
    max_source_compression_ratio: int = 100
    max_cv_pdf_bytes: int = 25 * 1024 * 1024
    cv_import_max_buffer_bytes: int = 64 * 1024 * 1024
    application_statuses: tuple[str, ...] = (
        "Draft",
        "Applied",
        "Interview",
        "Offer",
        "Rejected",
        "Withdrawn",
    )


def build_settings(project_root: Path | None = None) -> Settings:
    """Build a fully resolved immutable settings value for a project root."""

    root = (project_root or Path(__file__).resolve().parent).resolve()
    prompts_dir = root / "prompts"
    data_dir = root / "data"
    uploads_dir = data_dir / "uploads"
    cache_dir = data_dir / "cache"
    cv_dir = data_dir / "cv"
    return Settings(
        project_root=root,
        prompts_dir=prompts_dir,
        style_examples_dir=prompts_dir / "style_examples",
        data_dir=data_dir,
        source_library_dir=data_dir / "source_library",
        uploads_dir=uploads_dir,
        cache_dir=cache_dir,
        letters_dir=root / "letters",
        cv_dir=cv_dir,
        cv_versions_dir=cv_dir / "versions",
        cv_staging_dir=cv_dir / "staging",
        cv_active_path=cv_dir / "active.json",
        cv_pending_path=cv_dir / "pending.json",
        cv_pending_recovery_path=cv_dir / "pending.recovery.json",
        applications_path=data_dir / "applications.xlsx",
        system_prompt_cache_path=cache_dir / "last_system_prompt.md",
    )


def ensure_dirs(settings: Settings) -> None:
    """Create all runtime directories required by the supplied settings."""

    directories = (
        settings.prompts_dir,
        settings.style_examples_dir,
        settings.data_dir,
        settings.source_library_dir,
        settings.uploads_dir,
        settings.cache_dir,
        settings.cv_dir,
        settings.cv_versions_dir,
        settings.cv_staging_dir,
        settings.letters_dir,
    )
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
