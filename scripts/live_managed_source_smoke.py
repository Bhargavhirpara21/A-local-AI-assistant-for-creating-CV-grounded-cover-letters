"""Run authorized live checks against the active private source library."""

from __future__ import annotations

import argparse
import logging
from typing import cast

from config import build_settings
from core.cv_import import CvGenerationSelection, CvImportWorkflow
from core.generator import LetterGenerator, LetterOutput
from core.language import detect_language
from core.source_library import Language, SourceLibrary
from llm import get_client
from scripts.live_generation_smoke import SmokeCase, _smoke_cases, _word_count

LOGGER = logging.getLogger(__name__)


def main(
    confirmed: bool,
    language_filter: Language | None = None,
) -> int:
    """Run live generation only after explicit private-transmission consent."""

    if not confirmed:
        raise RuntimeError(
            "Explicit authorization is required before loading private sources."
        )
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    settings = build_settings()
    sources = SourceLibrary(settings)
    if not sources.is_ready():
        raise RuntimeError(
            "The managed source library is incomplete; import it before testing."
        )
    client = get_client(settings)
    cv_workflow = CvImportWorkflow(settings, client)
    cv_selection = cv_workflow.select_for_generation()
    generator = LetterGenerator(settings, sources, client)
    cases = tuple(
        case
        for case in _smoke_cases()
        if language_filter is None or case.language == language_filter
    )
    for case in cases:
        source_hash_before = sources.load_bundle(case.language).sha256
        output = generator.generate_letter(
            case.job_text,
            case.language,
            cv_selection=cv_selection,
        )
        _validate_managed_output(output, case, sources, cv_selection)
        source_hash_after = sources.load_bundle(case.language).sha256
        if source_hash_after != source_hash_before:
            raise RuntimeError(
                f"{case.language} managed sources changed during generation."
            )
        grounding = generator.check_grounding(
            output.letter,
            case.language,
            cv_selection=cv_selection,
        )
        if not grounding.ran:
            raise RuntimeError(
                f"{case.language} managed-source grounding could not execute."
            )
        if not grounding.ok:
            raise RuntimeError(
                f"{case.language} managed-source grounding found "
                f"{len(grounding.warnings)} warning(s)."
            )
        LOGGER.info(
            "Managed-source live %s passed: company=%s, role=%s, words=%d, "
            "verification_notes=%d, grounding=OK",
            case.language,
            output.company,
            output.role,
            _word_count(output.letter),
            len(output.verification_notes),
        )
    return 0


def _validate_managed_output(
    output: LetterOutput,
    case: SmokeCase,
    sources: SourceLibrary,
    cv_selection: CvGenerationSelection,
) -> None:
    if output.language != case.language:
        raise RuntimeError(f"{case.language} output used the wrong language.")
    if output.company != case.expected_company:
        raise RuntimeError(f"{case.language} output parsed the wrong company.")
    if case.expected_role_fragment.casefold() not in output.role.casefold():
        raise RuntimeError(f"{case.language} output parsed the wrong role.")
    expected_source_hash = sources.load_bundle(case.language).sha256
    if output.source_hash != expected_source_hash:
        raise RuntimeError(f"{case.language} output has the wrong source hash.")
    if output.trace is None:
        raise RuntimeError(f"{case.language} output has no exact-input trace.")
    if output.trace.source_hash != output.source_hash:
        raise RuntimeError(
            f"{case.language} trace is bound to the wrong source hash."
        )
    if output.trace.input_hash != output.input_hash:
        raise RuntimeError(f"{case.language} output has the wrong input hash.")
    if output.cv_version_id != cv_selection.cv_version_id:
        raise RuntimeError(f"{case.language} output has the wrong CV version.")
    if output.cv_reference_hash != cv_selection.cv_reference_hash:
        raise RuntimeError(
            f"{case.language} output has the wrong CV reference hash."
        )
    if output.used_previous_cv != cv_selection.used_previous_cv:
        raise RuntimeError(
            f"{case.language} output has the wrong previous-CV decision."
        )
    if output.trace.cv_version_id != output.cv_version_id:
        raise RuntimeError(f"{case.language} trace has the wrong CV version.")
    if output.trace.cv_reference_hash != output.cv_reference_hash:
        raise RuntimeError(
            f"{case.language} trace has the wrong CV reference hash."
        )
    if output.trace.used_previous_cv != output.used_previous_cv:
        raise RuntimeError(
            f"{case.language} trace has the wrong previous-CV decision."
        )
    if detect_language(output.letter) != case.language:
        raise RuntimeError(f"{case.language} output failed language detection.")
    parser_markers = (
        "structured metadata",
        "different language in its metadata",
        "unknown fit-assessment",
    )
    matched_markers = tuple(
        marker
        for marker in parser_markers
        if any(
            marker in note.casefold()
            for note in output.verification_notes
        )
    )
    if matched_markers:
        raise RuntimeError(
            f"{case.language} output failed structured parsing categories: "
            + ", ".join(matched_markers)
        )


def _parse_arguments() -> tuple[bool, Language | None]:
    parser = argparse.ArgumentParser(
        description=(
            "Generate against the active private source library after consent."
        )
    )
    parser.add_argument(
        "--confirm-private-source-transmission",
        action="store_true",
        required=True,
        help="Confirm that selected private sources may be sent to Claude.",
    )
    parser.add_argument(
        "--language",
        choices=("de", "en"),
        help="Run only one language; omit to test both.",
    )
    arguments = parser.parse_args()
    return (
        bool(arguments.confirm_private_source_transmission),
        cast(Language | None, arguments.language),
    )


if __name__ == "__main__":
    confirmation, selected_language = _parse_arguments()
    raise SystemExit(main(confirmation, selected_language))
