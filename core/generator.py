"""Dynamic prompt assembly, generation, research, and grounding workflows."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from config import Settings
from core.cv_import import CvGenerationSelection
from core.source_library import Language, SourceBundle, SourceLibrary
from core.url_safety import validate_http_url
from llm.base import LLMClient

LOGGER = logging.getLogger(__name__)

_FIT_LABELS: tuple[str, ...] = (
    "Strong match",
    "Reasonable match",
    "Stretch application",
    "Poor match",
)
_GENERATION_TRACE_DOMAIN = b"AutoCover.GenerationTrace.v3\0"
_MAX_STYLE_EXAMPLE_FILES = 2
_MAX_STYLE_TEXT_CHARS = 16_000
_METADATA_FIELDS: tuple[str, ...] = (
    "company",
    "role",
    "language",
    "contact_person",
    "reference_number",
    "location",
    "job_url",
    "fit_assessment",
    "fit_rationale",
    "verification_notes",
)
_RESEARCH_FIELDS: tuple[str, ...] = (
    "summary",
    "source_urls",
    "warnings",
)

GenerationOperation = Literal["generation", "refinement"]


def compute_generation_input_hash(
    operation: GenerationOperation,
    backend: str,
    model: str | None,
    source_hash: str,
    system_prompt: str,
    user_prompt: str,
    *,
    cv_version_id: str,
    cv_reference_hash: str,
    used_previous_cv: bool,
) -> str:
    """Return a deterministic SHA-256 hash of exact framed model inputs."""

    if not isinstance(used_previous_cv, bool):
        raise ValueError("The previous-CV provenance flag must be boolean.")
    framed = bytearray(_GENERATION_TRACE_DOMAIN)
    fields: tuple[tuple[str, str | None], ...] = (
        ("operation", operation),
        ("backend", backend),
        ("model", model),
        ("source_hash", source_hash),
        ("cv_version_id", cv_version_id),
        ("cv_reference_hash", cv_reference_hash),
        ("used_previous_cv", "true" if used_previous_cv else "false"),
        ("system_prompt", system_prompt),
        ("user_prompt", user_prompt),
    )
    for name, value in fields:
        encoded_name = name.encode("utf-8")
        encoded_value = (
            b"\x00"
            if value is None
            else b"\x01" + value.encode("utf-8")
        )
        framed.extend(len(encoded_name).to_bytes(4, byteorder="big"))
        framed.extend(encoded_name)
        framed.extend(len(encoded_value).to_bytes(8, byteorder="big"))
        framed.extend(encoded_value)
    return hashlib.sha256(framed).hexdigest()


@dataclass(frozen=True, slots=True)
class GenerationTrace:
    """Exact model invocation inputs and their deterministic provenance hash."""

    operation: GenerationOperation
    backend: str
    model: str | None
    source_hash: str
    cv_version_id: str
    cv_reference_hash: str
    used_previous_cv: bool
    system_prompt: str
    user_prompt: str
    input_hash: str


@dataclass(frozen=True, slots=True)
class LetterOutput:
    """Parsed cover letter and application metadata returned by the model."""

    letter: str
    company: str = "Unknown"
    role: str = "Unknown"
    language: Language = "de"
    contact_person: str | None = None
    reference_number: str | None = None
    location: str | None = None
    job_url: str | None = None
    fit_assessment: str = "Unassessed"
    fit_rationale: str = ""
    verification_notes: tuple[str, ...] = ()
    research_urls: tuple[str, ...] = ()
    source_hash: str = ""
    cv_version_id: str = ""
    cv_reference_hash: str = ""
    used_previous_cv: bool = False
    input_hash: str = ""
    trace: GenerationTrace | None = None
    raw: str = ""


@dataclass(frozen=True, slots=True)
class GroundingResult:
    """Outcome of checking candidate claims against the current source facts."""

    ran: bool
    ok: bool
    warnings: tuple[str, ...]
    raw: str = ""


@dataclass(frozen=True, slots=True)
class ResearchResult:
    """Bounded official-source research supplied to the generation step."""

    ran: bool
    summary: str
    source_urls: tuple[str, ...]
    warnings: tuple[str, ...]
    raw: str = ""


class GenerationError(RuntimeError):
    """Raised when a letter cannot be generated or parsed into usable text."""


class LetterGenerator:
    """Coordinate fresh source loading with a backend-neutral model client."""

    _settings: Settings
    _sources: SourceLibrary
    _client: LLMClient

    def __init__(
        self,
        settings: Settings,
        sources: SourceLibrary,
        client: LLMClient,
    ) -> None:
        """Initialize generation with explicit configuration and dependencies."""

        self._settings = settings
        self._sources = sources
        self._client = client

    def build_system_prompt(
        self,
        language: Language,
        *,
        cv_selection: CvGenerationSelection,
    ) -> tuple[str, SourceBundle]:
        """Build a fresh prompt and return the exact source snapshot it uses."""

        selection = self._require_cv_selection(cv_selection)
        bundle = self._sources.load_bundle(language)
        output_contract = self._read_prompt("output_contract.md")
        sections = [
            output_contract,
            (
                "# CURRENT SOURCE CONTROLLER\n\n"
                + bundle.instructions.text.rstrip()
            ),
            (
                "# MATCHING-LANGUAGE CANDIDATE PROFILE "
                "(primary candidate-fact source)\n\n"
                + bundle.profile.text.rstrip()
            ),
            (
                "# MATCHING-LANGUAGE MASTER LETTER LIBRARY "
                "(wording guidance, never an independent fact source)\n\n"
                + bundle.master_letter.text.rstrip()
            ),
        ]
        sections.append(
            "# REVIEWED CV REFERENCE (secondary, lower authority)\n\n"
            + selection.reference_markdown.rstrip()
        )
        style_examples = self._read_style_examples()
        if style_examples:
            sections.append(
                "# STYLE EXAMPLES (voice only, never candidate facts)\n\n"
                + style_examples
            )
        return "\n\n".join(sections).strip() + "\n", bundle

    def generate_letter(
        self,
        job_text: str,
        language: Language,
        *,
        cv_selection: CvGenerationSelection,
        notes: str = "",
        job_url: str = "",
        research: ResearchResult | None = None,
    ) -> LetterOutput:
        """Generate and parse one new letter from the current private sources."""

        selection = self._require_cv_selection(cv_selection)
        if not job_text.strip():
            raise GenerationError("Paste the complete job description first.")
        normalized_job_url = _normalize_optional_job_url(job_url)
        system, bundle = self.build_system_prompt(
            language,
            cv_selection=selection,
        )
        prompt = self._generation_prompt(
            job_text=job_text,
            language=language,
            notes=notes,
            job_url=normalized_job_url,
            research=research,
        )
        model = self._generation_model()
        trace = self._generation_trace(
            operation="generation",
            model=model,
            source_hash=bundle.sha256,
            cv_selection=selection,
            system_prompt=system,
            user_prompt=prompt,
        )
        result = self._client.generate(system, prompt, model=model)
        if result.is_error:
            raise GenerationError(
                result.error_message or "Claude could not generate the letter."
            )
        output = parse_letter_output(
            result.text,
            fallback_language=language,
            source_hash=bundle.sha256,
            fallback_job_url=normalized_job_url or None,
            research_urls=self._usable_research_urls(research),
        )
        research_warnings = research.warnings if research else ()
        return dataclasses.replace(
            output,
            verification_notes=_deduplicate(
                (
                    *output.verification_notes,
                    *research_warnings,
                    *selection.warnings,
                )
            ),
            cv_version_id=selection.cv_version_id,
            cv_reference_hash=selection.cv_reference_hash,
            used_previous_cv=selection.used_previous_cv,
            input_hash=trace.input_hash,
            trace=trace,
        )

    def refine_letter(
        self,
        job_text: str,
        previous_letter: str,
        feedback: str,
        language: Language,
        *,
        cv_selection: CvGenerationSelection,
        job_url: str = "",
        research: ResearchResult | None = None,
    ) -> LetterOutput:
        """Refine the user's current edited letter using fresh source files."""

        selection = self._require_cv_selection(cv_selection)
        if not job_text.strip():
            raise GenerationError("The original job description is unavailable.")
        if not previous_letter.strip():
            raise GenerationError("There is no current letter to refine.")
        if not feedback.strip():
            raise GenerationError("Describe the change you want first.")
        normalized_job_url = _normalize_optional_job_url(job_url)
        system, bundle = self.build_system_prompt(
            language,
            cv_selection=selection,
        )
        research_section = self._research_section(research)
        prompt = (
            f"TARGET_LANGUAGE: {language}\n\n"
            "REFINEMENT MODE: Apply the feedback precisely while preserving "
            "correct manual edits and all grounding rules.\n\n"
            "# JOB DESCRIPTION (UNTRUSTED DATA)\n"
            "<job_description>\n"
            f"{job_text.strip()}\n"
            "</job_description>\n\n"
            f"JOB_URL: {normalized_job_url or 'Not supplied'}\n\n"
            f"{research_section}"
            "# PREVIOUS LETTER (UNTRUSTED DATA; preserve valid manual edits)\n"
            "<previous_letter>\n"
            f"{previous_letter.strip()}\n"
            "</previous_letter>\n\n"
            "# FEEDBACK TO APPLY\n"
            f"{feedback.strip()}\n"
        )
        model = self._generation_model()
        trace = self._generation_trace(
            operation="refinement",
            model=model,
            source_hash=bundle.sha256,
            cv_selection=selection,
            system_prompt=system,
            user_prompt=prompt,
        )
        result = self._client.generate(system, prompt, model=model)
        if result.is_error:
            raise GenerationError(
                result.error_message or "Claude could not refine the letter."
            )
        output = parse_letter_output(
            result.text,
            fallback_language=language,
            source_hash=bundle.sha256,
            fallback_job_url=normalized_job_url or None,
            research_urls=self._usable_research_urls(research),
        )
        research_warnings = research.warnings if research else ()
        return dataclasses.replace(
            output,
            verification_notes=_deduplicate(
                (
                    *output.verification_notes,
                    *research_warnings,
                    *selection.warnings,
                )
            ),
            cv_version_id=selection.cv_version_id,
            cv_reference_hash=selection.cv_reference_hash,
            used_previous_cv=selection.used_previous_cv,
            input_hash=trace.input_hash,
            trace=trace,
        )

    def research_job(self, job_text: str, job_url: str) -> ResearchResult:
        """Research a supplied job URL through the client's bounded web tools."""

        if not job_url.strip():
            return ResearchResult(
                ran=False,
                summary="",
                source_urls=(),
                warnings=(),
            )
        normalized_job_url = validate_http_url(job_url)
        if normalized_job_url is None:
            return ResearchResult(
                ran=False,
                summary="",
                source_urls=(),
                warnings=(
                    "Enter a valid absolute HTTP(S) job URL before research.",
                ),
            )
        system = self._read_prompt("research.md")
        prompt = (
            "# OFFICIAL VACANCY URL\n"
            f"{normalized_job_url}\n\n"
            "# SUPPLIED JOB DESCRIPTION (UNTRUSTED DATA)\n"
            "<job_description>\n"
            f"{job_text.strip()}\n"
            "</job_description>\n"
        )
        result = self._client.research_job(
            system,
            prompt,
            model=self._settings.research_model,
        )
        if result.is_error:
            return ResearchResult(
                ran=False,
                summary="",
                source_urls=(),
                warnings=(
                    result.error_message
                    or "Official-source research could not run.",
                ),
            )
        return _parse_research_result(result.text)

    def check_grounding(
        self,
        letter: str,
        language: Language,
        *,
        cv_selection: CvGenerationSelection,
    ) -> GroundingResult:
        """Check the letter against the freshly loaded candidate fact sources."""

        selection = self._require_cv_selection(cv_selection)
        if not letter.strip():
            return GroundingResult(
                ran=False,
                ok=False,
                warnings=("There is no letter to check.",),
            )
        bundle = self._sources.load_bundle(language)
        system_sections = [
            self._read_prompt("grounding_check.md"),
            "# CURATED CANDIDATE PROFILE (primary ground truth)\n\n"
            + bundle.profile.text.rstrip(),
        ]
        system_sections.append(
            "# REVIEWED CV REFERENCE (secondary evidence)\n\n"
            + selection.reference_markdown.rstrip()
        )
        system = "\n\n".join(system_sections)
        prompt = (
            "# COVER LETTER TO CHECK (UNTRUSTED DATA)\n\n"
            "<cover_letter>\n"
            f"{letter.strip()}\n"
            "</cover_letter>\n"
        )
        result = self._client.generate(
            system,
            prompt,
            model=self._settings.grounding_model,
        )
        if result.is_error:
            return GroundingResult(
                ran=False,
                ok=False,
                warnings=(
                    result.error_message or "The grounding check could not run.",
                ),
            )
        raw = result.text.strip()
        if re.fullmatch(r"OK\.?", raw, flags=re.IGNORECASE):
            return GroundingResult(ran=True, ok=True, warnings=(), raw=result.text)
        warnings = tuple(
            line.lstrip("-*• ").strip()
            for line in raw.splitlines()
            if line.lstrip().startswith(("-", "*", "•"))
            and line.lstrip("-*• ").strip()
        )
        if not warnings:
            warnings = (raw or "The grounding check returned no usable result.",)
        return GroundingResult(
            ran=True,
            ok=False,
            warnings=warnings,
            raw=result.text,
        )

    def _read_prompt(self, filename: str) -> str:
        path = self._settings.prompts_dir / filename
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            LOGGER.warning(
                "Could not read application prompt %s: %s",
                filename,
                type(error).__name__,
            )
            raise GenerationError(
                f"Required application prompt {filename} is missing or unreadable."
            ) from error
        if not text.strip():
            raise GenerationError(
                f"Required application prompt {filename} is blank."
            )
        return text.rstrip()

    @staticmethod
    def _require_cv_selection(
        cv_selection: CvGenerationSelection,
    ) -> CvGenerationSelection:
        if not isinstance(cv_selection, CvGenerationSelection):
            raise GenerationError(
                "Select a confirmed CV before using this AI action."
            )
        return cv_selection

    def _read_style_examples(self) -> str:
        directory = self._settings.style_examples_dir
        if not directory.is_dir():
            return ""
        try:
            paths = sorted(
                (
                    path
                    for path in directory.iterdir()
                    if path.is_file()
                    and path.suffix.casefold() in (".md", ".txt")
                    and path.name.casefold() != "readme.md"
                ),
                key=lambda path: path.name.casefold(),
            )
        except OSError as error:
            LOGGER.warning(
                "Could not enumerate style examples directory: %s",
                type(error).__name__,
            )
            raise GenerationError(
                "The style examples directory is unreadable."
            ) from error
        examples: list[str] = []
        for path in paths[:_MAX_STYLE_EXAMPLE_FILES]:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                LOGGER.warning(
                    "Could not read style example %s: %s",
                    path.name,
                    type(error).__name__,
                )
                raise GenerationError(
                    f"Style example {path.name} is unreadable."
                ) from error
            if text.strip():
                separator = "\n\n" if examples else ""
                remaining = (
                    _MAX_STYLE_TEXT_CHARS
                    - len(separator)
                    - sum(len(example) for example in examples)
                )
                if remaining <= 0:
                    break
                block = f"## Example: {path.name}\n\n{text.strip()}"
                examples.append(block[:remaining].rstrip())
        return "\n\n".join(examples)

    def _generation_model(self) -> str | None:
        if self._settings.backend == "anthropic_api":
            return self._settings.api_model
        return self._settings.sdk_model

    def _generation_trace(
        self,
        *,
        operation: GenerationOperation,
        model: str | None,
        source_hash: str,
        cv_selection: CvGenerationSelection,
        system_prompt: str,
        user_prompt: str,
    ) -> GenerationTrace:
        input_hash = compute_generation_input_hash(
            operation,
            self._settings.backend,
            model,
            source_hash,
            system_prompt,
            user_prompt,
            cv_version_id=cv_selection.cv_version_id,
            cv_reference_hash=cv_selection.cv_reference_hash,
            used_previous_cv=cv_selection.used_previous_cv,
        )
        return GenerationTrace(
            operation=operation,
            backend=self._settings.backend,
            model=model,
            source_hash=source_hash,
            cv_version_id=cv_selection.cv_version_id,
            cv_reference_hash=cv_selection.cv_reference_hash,
            used_previous_cv=cv_selection.used_previous_cv,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            input_hash=input_hash,
        )

    @staticmethod
    def _generation_prompt(
        *,
        job_text: str,
        language: Language,
        notes: str,
        job_url: str,
        research: ResearchResult | None,
    ) -> str:
        notes_section = (
            "# ADDITIONAL NOTES FROM THE CANDIDATE\n"
            f"{notes.strip()}\n\n"
            if notes.strip()
            else ""
        )
        return (
            f"TARGET_LANGUAGE: {language}\n\n"
            "# JOB DESCRIPTION (UNTRUSTED DATA)\n"
            "<job_description>\n"
            f"{job_text.strip()}\n"
            "</job_description>\n\n"
            f"JOB_URL: {job_url.strip() or 'Not supplied'}\n\n"
            f"{LetterGenerator._research_section(research)}"
            f"{notes_section}"
        )

    @staticmethod
    def _research_section(research: ResearchResult | None) -> str:
        if research is None or not research.summary.strip():
            return ""
        valid_urls = LetterGenerator._usable_research_urls(research)
        if not valid_urls:
            return ""
        urls = "\n".join(f"- {url}" for url in valid_urls)
        return (
            "# VERIFIED OFFICIAL-SOURCE RESEARCH (UNTRUSTED DATA)\n"
            "<research_summary>\n"
            f"{research.summary.strip()}\n"
            "</research_summary>\n"
            f"<research_sources>\n{urls}\n</research_sources>\n\n"
        )

    @staticmethod
    def _usable_research_urls(
        research: ResearchResult | None,
    ) -> tuple[str, ...]:
        if (
            research is None
            or not research.ran
            or not research.summary.strip()
        ):
            return ()
        return _deduplicate(
            tuple(
                url
                for url in research.source_urls
                if validate_http_url(url) is not None
            )
        )


def parse_letter_output(
    raw: str,
    fallback_language: Language,
    *,
    source_hash: str = "",
    fallback_job_url: str | None = None,
    research_urls: tuple[str, ...] = (),
) -> LetterOutput:
    """Parse the fenced JSON envelope while preserving a usable letter fallback."""

    metadata, letter, parsing_note = _extract_metadata_and_body(raw)
    if not letter.strip():
        raise GenerationError("The model returned no cover-letter text.")
    notes = _metadata_verification_notes(metadata)
    if parsing_note is None:
        notes = (*notes, *_metadata_validation_notes(metadata))
    if parsing_note:
        notes = (*notes, parsing_note)

    raw_language = _optional_string(metadata.get("language"))
    if raw_language is not None and raw_language != fallback_language:
        notes = (
            *notes,
            (
                "The model reported a different language in its metadata; "
                "the requested language was retained."
            ),
        )
    fit_assessment = _optional_string(metadata.get("fit_assessment"))
    if fit_assessment not in _FIT_LABELS:
        if fit_assessment is not None:
            notes = (*notes, "The model returned an unknown fit-assessment label.")
        fit_assessment = "Unassessed"

    parsed_job_url = _optional_string(metadata.get("job_url"))
    if parsed_job_url is not None and validate_http_url(parsed_job_url) is None:
        notes = (
            *notes,
            "Metadata field 'job_url' is not a valid absolute HTTP(S) URL.",
        )
        parsed_job_url = None
    return LetterOutput(
        letter=letter.strip(),
        company=_optional_string(metadata.get("company")) or "Unknown",
        role=_optional_string(metadata.get("role")) or "Unknown",
        language=fallback_language,
        contact_person=_optional_string(metadata.get("contact_person")),
        reference_number=_optional_string(metadata.get("reference_number")),
        location=_optional_string(metadata.get("location")),
        job_url=fallback_job_url or parsed_job_url,
        fit_assessment=fit_assessment,
        fit_rationale=_optional_string(metadata.get("fit_rationale")) or "",
        verification_notes=_deduplicate(notes),
        research_urls=_deduplicate(research_urls),
        source_hash=source_hash,
        raw=raw,
    )


def _extract_metadata_and_body(raw: str) -> tuple[dict[str, Any], str, str | None]:
    match = re.match(
        r"^\s*```(?:json)?\s*\n(?P<metadata>.*?)\n```\s*(?P<body>.*)\Z",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match is None:
        return (
            {},
            raw,
            "The model response omitted the structured metadata envelope.",
        )
    body = match.group("body")
    try:
        parsed = json.loads(match.group("metadata"))
    except json.JSONDecodeError:
        return (
            {},
            body,
            "The model returned invalid structured metadata.",
        )
    if not isinstance(parsed, dict):
        return (
            {},
            body,
            "The model returned non-object structured metadata.",
        )
    return parsed, body, None


def _metadata_verification_notes(metadata: dict[str, Any]) -> tuple[str, ...]:
    value = metadata.get("verification_notes")
    if not isinstance(value, list):
        return ()
    return tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )


def _metadata_validation_notes(metadata: dict[str, Any]) -> tuple[str, ...]:
    notes: list[str] = []
    for field_name in _METADATA_FIELDS:
        if field_name not in metadata:
            notes.append(
                f"Structured metadata is missing required field '{field_name}'."
            )

    required_strings = (
        "company",
        "role",
        "language",
        "fit_assessment",
        "fit_rationale",
    )
    for field_name in required_strings:
        if field_name in metadata and not isinstance(metadata[field_name], str):
            notes.append(
                f"Structured metadata field '{field_name}' has the wrong type; "
                "expected a string."
            )

    nullable_strings = (
        "contact_person",
        "reference_number",
        "location",
        "job_url",
    )
    for field_name in nullable_strings:
        value = metadata.get(field_name)
        if field_name in metadata and value is not None and not isinstance(value, str):
            notes.append(
                f"Structured metadata field '{field_name}' has the wrong type; "
                "expected a string or null."
            )

    language = metadata.get("language")
    if isinstance(language, str) and language not in ("de", "en"):
        notes.append(
            "Structured metadata field 'language' must be either 'de' or 'en'."
        )

    verification_notes = metadata.get("verification_notes")
    if (
        "verification_notes" in metadata
        and (
            not isinstance(verification_notes, list)
            or any(not isinstance(item, str) for item in verification_notes)
        )
    ):
        notes.append(
            "Structured metadata field 'verification_notes' has the wrong type; "
            "expected a list of strings."
        )
    return tuple(notes)


def _parse_research_result(raw: str) -> ResearchResult:
    match = re.fullmatch(
        r"\s*```(?:json)?\s*\n(?P<payload>.*?)\n```\s*",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    payload = match.group("payload") if match is not None else raw.strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return ResearchResult(
            ran=True,
            summary="",
            source_urls=(),
            warnings=(
                "Research returned unstructured output; no summary was used.",
            ),
            raw=raw,
        )
    if not isinstance(parsed, dict):
        return ResearchResult(
            ran=True,
            summary="",
            source_urls=(),
            warnings=("Research returned invalid structured output.",),
            raw=raw,
        )
    schema_notes = list(_research_validation_notes(parsed))
    candidate_urls = _json_string_tuple(parsed.get("source_urls"))
    urls = tuple(
        value
        for value in candidate_urls
        if validate_http_url(value) is not None
    )
    evidence_notes: list[str] = []
    if len(urls) != len(candidate_urls):
        evidence_notes.append(
            "Research source_urls contained an invalid absolute HTTP(S) URL."
        )
    if not urls:
        evidence_notes.append(
            "Research returned no valid official HTTP(S) source URL; "
            "no summary was used."
        )
    summary = _optional_string(parsed.get("summary")) or ""
    if not summary:
        evidence_notes.append(
            "Research returned no usable summary."
        )
    schema_valid = not schema_notes
    usable_summary = summary if schema_valid and urls and summary else ""
    return ResearchResult(
        ran=True,
        summary=usable_summary,
        source_urls=_deduplicate(urls) if schema_valid else (),
        warnings=_deduplicate(
            (
                *_json_string_tuple(parsed.get("warnings")),
                *schema_notes,
                *evidence_notes,
            )
        ),
        raw=raw,
    )


def _research_validation_notes(metadata: dict[str, Any]) -> tuple[str, ...]:
    notes: list[str] = []
    for field_name in _RESEARCH_FIELDS:
        if field_name not in metadata:
            notes.append(
                f"Research output is missing required field '{field_name}'."
            )
    if "summary" in metadata and not isinstance(metadata["summary"], str):
        notes.append(
            "Research field 'summary' has the wrong type; expected a string."
        )
    for field_name in ("source_urls", "warnings"):
        value = metadata.get(field_name)
        if (
            field_name in metadata
            and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)
            )
        ):
            notes.append(
                f"Research field '{field_name}' has the wrong type; "
                "expected a list of strings."
            )
    return tuple(notes)


def _json_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        item.strip()
        for item in value
        if isinstance(item, str) and item.strip()
    )


def _optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip().strip("\"'")
    if not stripped or stripped.casefold() in ("null", "none", "unknown"):
        return None
    return stripped


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, list):
        return tuple(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    return ()


def _deduplicate(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _normalize_optional_job_url(value: str) -> str:
    if not value.strip():
        return ""
    normalized = validate_http_url(value)
    if normalized is None:
        raise GenerationError(
            "Enter a valid absolute HTTP(S) job URL or leave it blank."
        )
    return normalized
