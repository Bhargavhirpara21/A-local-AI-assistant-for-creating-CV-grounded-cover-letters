"""UTF-8 persistence for immutable cover letters and exact-input traces."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from config import Settings
from core.url_safety import validate_http_url

if TYPE_CHECKING:
    from core.generator import LetterOutput


LOGGER = logging.getLogger(__name__)

_WINDOWS_ILLEGAL_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "AUX",
        "CLOCK$",
        "CON",
        "CONIN$",
        "CONOUT$",
        "NUL",
        "PRN",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)
_MAX_COMPONENT_LENGTH = 60
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ArchiveError(RuntimeError):
    """Raised when a letter cannot be validated or archived safely."""


def sanitize_filename_component(value: str) -> str:
    """Return a stable Windows-safe filename component of at most 60 characters."""

    normalized = unicodedata.normalize("NFC", value)
    without_invalid = "".join(
        character
        for character in normalized
        if character not in _WINDOWS_ILLEGAL_CHARACTERS
        and unicodedata.category(character) != "Cc"
    )
    collapsed = re.sub(r"\s+", "-", without_invalid.strip())
    cleaned = collapsed.rstrip(" .")
    if cleaned in ("", ".", ".."):
        return "Unknown"

    cleaned = cleaned[:_MAX_COMPONENT_LENGTH].rstrip(" .")
    if cleaned in ("", ".", ".."):
        return "Unknown"

    device_stem = cleaned.split(".", maxsplit=1)[0].upper()
    if device_stem in _WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"[:_MAX_COMPONENT_LENGTH].rstrip(" .")
    return cleaned or "Unknown"


class LetterArchive:
    """Persist matched, immutable Markdown and generation-trace snapshots."""

    _settings: Settings
    _clock: Callable[[], datetime]

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize an archive with explicit settings and an optional clock."""

        self._settings = settings
        self._clock = clock or (lambda: datetime.now().astimezone())

    def save_letter(
        self,
        output: LetterOutput,
        job_text: str,
        application_id: str,
        *,
        refined: bool = False,
        research_urls: Sequence[str] = (),
    ) -> Path:
        """Atomically save a letter/trace pair and return the Markdown path."""

        letter = _canonicalize_newlines(output.letter)
        posting = _canonicalize_newlines(job_text)
        normalized_application_id = application_id.strip()
        if not letter.strip():
            raise ArchiveError("Cannot archive an empty cover letter.")
        if not posting.strip():
            raise ArchiveError("Cannot archive a letter without its job description.")
        if not normalized_application_id:
            raise ArchiveError("Cannot archive a letter without an application ID.")

        trace_snapshot = _validated_trace_snapshot(output, refined=refined)
        generated_at = self._current_time()
        normalized_urls = _normalize_research_urls(
            getattr(output, "research_urls", ()),
            research_urls,
        )
        verification_notes = _normalize_text_sequence(
            getattr(output, "verification_notes", ()),
            label="Verification notes",
        )
        content = _render_archive(
            output=output,
            letter=letter,
            job_text=posting,
            application_id=normalized_application_id,
            generated_at=generated_at,
            refined=refined,
            verification_notes=verification_notes,
            research_urls=normalized_urls,
        )
        trace_content = json.dumps(
            trace_snapshot,
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        stem = _archive_stem(output, generated_at, refined=refined)
        path = self._publish_new_pair(stem, content, trace_content)
        LOGGER.info(
            "Archived letter and trace snapshots for application %s",
            normalized_application_id,
        )
        return path

    def _current_time(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ArchiveError("The archive clock must return a timezone-aware datetime.")
        return value

    def _publish_new_pair(
        self,
        stem: str,
        letter_content: str,
        trace_content: str,
    ) -> Path:
        directory = self._settings.letters_dir
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            LOGGER.exception("Could not create the private letter archive directory")
            raise ArchiveError(
                "Could not archive the letter. Check the letters directory permissions "
                "and try again."
            ) from error

        letter_temporary_path: Path | None = None
        trace_temporary_path: Path | None = None
        try:
            letter_temporary_path = _write_temporary_file(
                directory,
                prefix=".letter-",
                content=letter_content,
            )
            trace_temporary_path = _write_temporary_file(
                directory,
                prefix=".trace-",
                content=trace_content,
            )

            collision_number = 1
            while True:
                suffix = "" if collision_number == 1 else f"_{collision_number}"
                letter_candidate = directory / f"{stem}{suffix}.md"
                trace_candidate = directory / f"{stem}{suffix}.trace.json"
                try:
                    # Publish the private trace first. The Markdown file is the
                    # commit marker, so consumers never see a letter without a
                    # corresponding complete trace.
                    os.link(trace_temporary_path, trace_candidate)
                except FileExistsError:
                    collision_number += 1
                    continue
                except OSError as error:
                    LOGGER.exception("Could not atomically publish generation trace")
                    raise ArchiveError(
                        "Could not archive the letter atomically. Check the letters "
                        "directory permissions and try again."
                    ) from error

                try:
                    os.link(letter_temporary_path, letter_candidate)
                except FileExistsError:
                    _rollback_published_trace(trace_candidate)
                    collision_number += 1
                    continue
                except OSError as error:
                    _rollback_published_trace(trace_candidate)
                    LOGGER.exception("Could not atomically publish letter archive")
                    raise ArchiveError(
                        "Could not archive the letter atomically. Check the letters "
                        "directory permissions and try again."
                    ) from error
                return letter_candidate
        except ArchiveError:
            raise
        except OSError as error:
            LOGGER.exception("Could not write temporary archive files")
            raise ArchiveError(
                "Could not archive the letter. Check available disk space and "
                "directory permissions, then try again."
            ) from error
        finally:
            _remove_temporary_file(letter_temporary_path)
            _remove_temporary_file(trace_temporary_path)


def save_letter(
    output: LetterOutput,
    job_text: str,
    application_id: str,
    settings: Settings,
    *,
    refined: bool = False,
    research_urls: Sequence[str] = (),
    clock: Callable[[], datetime] | None = None,
) -> Path:
    """Save a letter through a short-lived archive using explicit dependencies."""

    return LetterArchive(settings, clock=clock).save_letter(
        output,
        job_text,
        application_id,
        refined=refined,
        research_urls=research_urls,
    )


def _canonicalize_newlines(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_text_sequence(
    values: Sequence[str],
    *,
    label: str,
) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise ArchiveError(f"{label} must be supplied as a sequence of text.")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ArchiveError(f"Every {label.lower()} entry must be text.")
        text = value.strip()
        if text and text not in seen:
            normalized.append(text)
            seen.add(text)
    return tuple(normalized)


def _normalize_research_urls(
    *url_groups: Sequence[str],
) -> tuple[str, ...]:
    merged: list[str] = []
    for urls in url_groups:
        merged.extend(
            _normalize_text_sequence(
                urls,
                label="Research URLs",
            )
        )
    normalized = _normalize_text_sequence(merged, label="Research URLs")
    if any(validate_http_url(url) is None for url in normalized):
        raise ArchiveError(
            "Every research URL must be a valid absolute HTTP(S) URL."
        )
    return normalized


def _validated_trace_snapshot(
    output: LetterOutput,
    *,
    refined: bool,
) -> dict[str, str | bool | None]:
    from core.generator import compute_generation_input_hash

    source_hash = _validated_sha256(
        getattr(output, "source_hash", None),
        label="source hash",
    )
    output_input_hash = _validated_sha256(
        getattr(output, "input_hash", None),
        label="input hash",
    )
    trace = getattr(output, "trace", None)
    if trace is None:
        raise ArchiveError("Cannot archive a letter without its generation trace.")

    operation = _required_trace_text(trace, "operation")
    expected_operation = "refinement" if refined else "generation"
    if operation != expected_operation:
        raise ArchiveError(
            "The generation trace operation does not match the archive operation."
        )
    backend = _required_trace_text(trace, "backend")
    system_prompt = _required_trace_text(trace, "system_prompt")
    user_prompt = _required_trace_text(trace, "user_prompt")
    if not hasattr(trace, "model"):
        raise ArchiveError("The generation trace is missing its model field.")
    model = getattr(trace, "model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ArchiveError("The generation trace model must be text or null.")

    trace_input_hash = _validated_sha256(
        getattr(trace, "input_hash", None),
        label="trace input hash",
    )
    if trace_input_hash != output_input_hash:
        raise ArchiveError(
            "The output input hash does not match its generation trace."
        )
    trace_source_hash = _validated_sha256(
        getattr(trace, "source_hash", None),
        label="trace source hash",
    )
    if trace_source_hash != source_hash:
        raise ArchiveError(
            "The output source hash does not match its generation trace."
        )
    output_cv_version_id = _required_archive_text(
        getattr(output, "cv_version_id", None),
        label="CV version ID",
    )
    trace_cv_version_id = _required_trace_text(trace, "cv_version_id")
    if trace_cv_version_id != output_cv_version_id:
        raise ArchiveError(
            "The output CV version ID does not match its generation trace."
        )
    output_cv_reference_hash = _validated_sha256(
        getattr(output, "cv_reference_hash", None),
        label="CV reference hash",
    )
    trace_cv_reference_hash = _validated_sha256(
        getattr(trace, "cv_reference_hash", None),
        label="trace CV reference hash",
    )
    if trace_cv_reference_hash != output_cv_reference_hash:
        raise ArchiveError(
            "The output CV reference hash does not match its generation trace."
        )
    output_used_previous_cv = _validated_bool(
        getattr(output, "used_previous_cv", None),
        label="output previous-CV flag",
    )
    trace_used_previous_cv = _validated_bool(
        getattr(trace, "used_previous_cv", None),
        label="trace previous-CV flag",
    )
    if trace_used_previous_cv != output_used_previous_cv:
        raise ArchiveError(
            "The output previous-CV flag does not match its generation trace."
        )
    calculated_hash = compute_generation_input_hash(
        operation,
        backend,
        model,
        trace_source_hash,
        system_prompt,
        user_prompt,
        cv_version_id=trace_cv_version_id,
        cv_reference_hash=trace_cv_reference_hash,
        used_previous_cv=trace_used_previous_cv,
    )
    if trace_input_hash != calculated_hash:
        raise ArchiveError(
            "The generation trace input hash does not verify against its exact inputs."
        )

    return {
        "operation": operation,
        "backend": backend,
        "model": model,
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "source_hash": source_hash,
        "cv_version_id": output_cv_version_id,
        "cv_reference_hash": output_cv_reference_hash,
        "used_previous_cv": output_used_previous_cv,
        "input_hash": output_input_hash,
    }


def _required_trace_text(trace: object, field_name: str) -> str:
    value = getattr(trace, field_name, None)
    if not isinstance(value, str) or not value.strip():
        readable_name = field_name.replace("_", " ")
        raise ArchiveError(
            f"The generation trace {readable_name} must be non-empty text."
        )
    return value


def _required_archive_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArchiveError(f"The archive {label} must be non-empty text.")
    return value


def _validated_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ArchiveError(f"The archive {label} must be boolean.")
    return value


def _validated_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ArchiveError(f"The archive {label} must be a lowercase SHA-256 hash.")
    return value


def _archive_stem(
    output: LetterOutput,
    generated_at: datetime,
    *,
    refined: bool,
) -> str:
    parts = (
        generated_at.strftime("%Y-%m-%d"),
        sanitize_filename_component(output.company),
        sanitize_filename_component(output.role),
    )
    prefix = "_".join(parts)
    refinement = "_refined" if refined else ""
    return f"{prefix}{refinement}_{generated_at.strftime('%H%M')}"


def _render_archive(
    *,
    output: LetterOutput,
    letter: str,
    job_text: str,
    application_id: str,
    generated_at: datetime,
    refined: bool,
    verification_notes: tuple[str, ...],
    research_urls: tuple[str, ...],
) -> str:
    frontmatter = [
        "---",
        f"application_id: {_yaml_scalar(application_id)}",
        f"company: {_yaml_scalar(output.company)}",
        f"role: {_yaml_scalar(output.role)}",
        f"language: {_yaml_scalar(output.language)}",
        f"contact_person: {_yaml_scalar(output.contact_person)}",
        f"reference_number: {_yaml_scalar(output.reference_number)}",
        f"location: {_yaml_scalar(output.location)}",
        f"job_url: {_yaml_scalar(output.job_url)}",
        f"fit_assessment: {_yaml_scalar(output.fit_assessment)}",
        f"fit_rationale: {_yaml_scalar(output.fit_rationale)}",
        f"generated_at: {_yaml_scalar(generated_at.isoformat())}",
        f"refined: {'true' if refined else 'false'}",
        f"source_hash: {_yaml_scalar(output.source_hash)}",
        f"input_hash: {_yaml_scalar(output.input_hash)}",
        f"cv_version_id: {_yaml_scalar(output.cv_version_id)}",
        f"cv_reference_hash: {_yaml_scalar(output.cv_reference_hash)}",
        f"used_previous_cv: {'true' if output.used_previous_cv else 'false'}",
    ]
    _append_yaml_sequence(
        frontmatter,
        key="verification_notes",
        values=verification_notes,
    )
    _append_yaml_sequence(
        frontmatter,
        key="research_urls",
        values=research_urls,
    )
    frontmatter.append("---")

    content = "\n".join(frontmatter) + "\n\n" + letter
    if not content.endswith("\n"):
        content += "\n"
    content += "\n<details><summary>Job description used</summary>\n\n"
    content += job_text
    if not content.endswith("\n"):
        content += "\n"
    content += "\n</details>\n"
    return content


def _append_yaml_sequence(
    lines: list[str],
    *,
    key: str,
    values: tuple[str, ...],
) -> None:
    if values:
        lines.append(f"{key}:")
        lines.extend(f"  - {_yaml_scalar(value)}" for value in values)
    else:
        lines.append(f"{key}: []")


def _write_temporary_file(
    directory: Path,
    *,
    prefix: str,
    content: str,
) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=prefix,
        suffix=".tmp",
        dir=directory,
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        return Path(temporary.name)


def _rollback_published_trace(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        LOGGER.exception("Could not roll back a partially published trace")
        raise ArchiveError(
            "Could not roll back a partial archive. Check the letters directory "
            "permissions before trying again."
        ) from error


def _remove_temporary_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        LOGGER.warning("Could not remove a temporary private archive file")


def _yaml_scalar(value: str | None) -> str:
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)
