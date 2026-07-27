"""Run privacy-safe German and English generation acceptance checks."""

from __future__ import annotations

import argparse
import logging
import re
import tempfile
import zipfile
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import cast

from config import Settings, build_settings, ensure_dirs
from core.cv_import import CvGenerationSelection, compute_cv_reference_hash
from core.generator import LetterGenerator, LetterOutput
from core.language import detect_language
from core.source_library import Language, SourceLibrary
from llm import get_client

LOGGER = logging.getLogger(__name__)

_MIN_SMOKE_LETTER_WORDS = 150
_MAX_SMOKE_LETTER_WORDS = 500


@dataclass(frozen=True, slots=True)
class SmokeCase:
    """One synthetic vacancy and its expected output characteristics."""

    language: Language
    job_text: str
    expected_company: str
    expected_role_fragment: str
    required_phrases: tuple[str, ...]
    banned_phrases: tuple[str, ...]


def main(language_filter: Language | None = None) -> int:
    """Generate two fictional letters without loading the user's private sources."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    with tempfile.TemporaryDirectory(
        prefix="autocover-live-smoke-"
    ) as temporary_directory:
        settings = _isolated_settings(Path(temporary_directory))
        ensure_dirs(settings)
        sources = SourceLibrary(settings)
        sources.import_zip(
            _synthetic_source_archive(),
            archive_name="fictional-smoke-sources.zip",
        )
        client = get_client(settings)
        generator = LetterGenerator(settings, sources, client)
        cv_selection = _synthetic_cv_selection()
        cases = tuple(
            case
            for case in _smoke_cases()
            if language_filter is None or case.language == language_filter
        )
        for case in cases:
            output = generator.generate_letter(
                case.job_text,
                case.language,
                cv_selection=cv_selection,
            )
            _validate_output(output, case, sources, cv_selection)
            grounding = generator.check_grounding(
                output.letter,
                case.language,
                cv_selection=cv_selection,
            )
            if not grounding.ran:
                raise RuntimeError(
                    f"{case.language} grounding check could not execute."
                )
            if not grounding.ok:
                raise RuntimeError(
                    f"{case.language} grounding check found "
                    f"{len(grounding.warnings)} warning(s): "
                    + " | ".join(grounding.warnings)
                )
            LOGGER.info(
                "Live %s generation passed: company=%s, role=%s, words=%d, "
                "grounding=OK",
                case.language,
                output.company,
                output.role,
                _word_count(output.letter),
            )
    return 0


def _isolated_settings(temporary_root: Path) -> Settings:
    base = build_settings()
    private_data = temporary_root / "data"
    uploads = private_data / "uploads"
    cache = private_data / "cache"
    cv_dir = private_data / "cv"
    return replace(
        base,
        style_examples_dir=temporary_root / "style_examples",
        data_dir=private_data,
        source_library_dir=private_data / "source_library",
        uploads_dir=uploads,
        cache_dir=cache,
        letters_dir=temporary_root / "letters",
        cv_dir=cv_dir,
        cv_versions_dir=cv_dir / "versions",
        cv_staging_dir=cv_dir / "staging",
        cv_active_path=cv_dir / "active.json",
        cv_pending_path=cv_dir / "pending.json",
        cv_pending_recovery_path=cv_dir / "pending.recovery.json",
        applications_path=private_data / "applications.xlsx",
        system_prompt_cache_path=cache / "last_system_prompt.md",
    )


def _synthetic_cv_selection() -> CvGenerationSelection:
    """Return an explicit validated CV selection containing fictional data only."""

    reference_markdown = (
        "# Profile\n\n"
        "Fictional CV reference for pipeline testing. "
        "Avery Morgan is a fictional junior software engineer based in Munich. "
        "This synthetic reference exists only to verify explicit CV selection "
        "and contains no personal project data.\n"
    )
    return CvGenerationSelection(
        cv_version_id="fictional-smoke-cv-v1",
        reference_markdown=reference_markdown,
        cv_reference_hash=compute_cv_reference_hash(reference_markdown),
        used_previous_cv=False,
        warnings=(),
    )


def _synthetic_source_archive() -> bytes:
    documents = {
        "cover_letter_instructions.md": (
            "# Fictional smoke-test controller\n\n"
            "Write only in TARGET_LANGUAGE. Follow the machine-readable output "
            "contract. The employer-facing letter must contain "
            f"{_MIN_SMOKE_LETTER_WORDS} to {_MAX_SMOKE_LETTER_WORDS} words; "
            "target 240 to 280 words. Use five concise paragraphs and make only "
            "claims found in the matching candidate profile. Never invent facts "
            "or copy requirements as candidate experience. Use a direct, "
            "professional tone.\n\n"
            "For German, start with `Bewerbung als <role>`, use `Sehr geehrte "
            "Damen und Herren,` when no person is named, and end with `Mit "
            "freundlichen Grüßen` plus the candidate name. For English, start "
            "with `Application for <role>`, use `Dear Hiring Team,` when no "
            "person is named, and end with `Kind regards,` plus the candidate "
            "name.\n\n"
            "Do not use `Hiermit bewerbe ich mich`, `Mit großem Interesse`, "
            "`I am excited to apply`, `I am passionate about`, `perfect fit`, "
            "or claims of perfect suitability."
        ),
        "bhargav_candidate_profile_en.md": (
            "# Avery Morgan — fictional test candidate\n\n"
            "Avery Morgan is a junior software engineer based in Munich. Avery "
            "earned an MSc in Software Engineering in 2025. In a twelve-month "
            "university-industry project, Avery developed and tested offline "
            "desktop tools for manufacturing engineers. The work used C++17, "
            "CMake, OpenCV, geometry algorithms, CAD/CAM data, and technical "
            "drawings. Avery also built C# and .NET desktop components with WPF "
            "for reviewing drawing metadata. Avery wrote unit and integration "
            "tests, documented design decisions, and worked directly with "
            "product engineers. Avery speaks professional English and B2 German. "
            "Avery values clear interfaces, traceable decisions, reliable "
            "software, and constructive cross-functional collaboration."
        ),
        "bhargav_candidate_profile_de.md": (
            "# Avery Morgan — fiktive Testperson\n\n"
            "Avery Morgan ist Junior-Softwareentwickler mit Wohnsitz in München "
            "und hat 2025 einen Masterabschluss in Software Engineering "
            "erworben. In einem zwölfmonatigen Hochschul-Industrieprojekt "
            "entwickelte und testete Avery lokale Desktop-Werkzeuge für "
            "Fertigungsingenieure. Dabei nutzte Avery C++17, CMake, OpenCV, "
            "Geometriealgorithmen, CAD/CAM-Daten und technische Zeichnungen. "
            "Avery entwickelte außerdem Desktop-Komponenten mit C#, .NET und WPF "
            "zur Prüfung von Zeichnungsmetadaten. Avery schrieb Unit- und "
            "Integrationstests, dokumentierte Entwurfsentscheidungen und "
            "arbeitete direkt mit Produktingenieuren zusammen. Avery spricht "
            "Englisch auf professionellem Niveau und Deutsch auf B2-Niveau. "
            "Avery legt Wert auf klare Schnittstellen, nachvollziehbare "
            "Entscheidungen, zuverlässige Software und konstruktive "
            "fachübergreifende Zusammenarbeit."
        ),
        "master_cover_letter_en.md": (
            "# Fictional English style library\n\n"
            "Use a concrete opening that connects the role's engineering problem "
            "to one verified project. Link two or three requirements to precise "
            "profile evidence. Acknowledge material gaps honestly. Close with "
            "the contribution the candidate can make and an invitation to talk. "
            "Prefer short sentences and specific nouns over promotional claims."
        ),
        "master_cover_letter_de.md": (
            "# Fiktive deutsche Stilbibliothek\n\n"
            "Beginne konkret mit der technischen Aufgabe der Stelle und einem "
            "belegten Projekt. Verknüpfe zwei oder drei Anforderungen mit "
            "präzisen Fakten aus dem Profil. Benenne wesentliche Lücken ehrlich. "
            "Schließe mit dem möglichen Beitrag und einer Gesprächseinladung. "
            "Nutze kurze Sätze und konkrete Begriffe statt Werbesprache."
        ),
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for filename, text in documents.items():
            archive.writestr(filename, text.encode("utf-8"))
    return buffer.getvalue()


def _smoke_cases() -> tuple[SmokeCase, ...]:
    return (
        SmokeCase(
            language="de",
            expected_company="Example Maschinenbau GmbH",
            expected_role_fragment="Softwareentwickler",
            required_phrases=(
                "Bewerbung als",
                "Sehr geehrte Damen und Herren,",
                "Mit freundlichen Grüßen",
            ),
            banned_phrases=(
                "Hiermit bewerbe ich mich",
                "Mit großem Interesse",
                "perfekt zu Ihnen passe",
            ),
            job_text=(
                "Die Example Maschinenbau GmbH entwickelt Software für die "
                "Prüfung technischer Zeichnungen in der industriellen Fertigung. "
                "Für unseren Standort Stuttgart suchen wir einen Junior "
                "Softwareentwickler C#/.NET (m/w/d). Sie entwickeln Desktop-"
                "Anwendungen mit C#, .NET und WPF, integrieren CAD-Daten und "
                "arbeiten mit Konstruktion und Produktmanagement zusammen. "
                "Erfahrung mit technischen Zeichnungen, sauberer Software-"
                "architektur und automatisierten Tests ist von Vorteil. Wir "
                "erwarten Deutschkenntnisse und Englisch für die Zusammenarbeit "
                "in internationalen Projekten. Eine Kontaktperson ist in dieser "
                "Ausschreibung nicht genannt."
            ),
        ),
        SmokeCase(
            language="en",
            expected_company="Example Industrial Systems GmbH",
            expected_role_fragment="C++ Software Engineer",
            required_phrases=("Application for", "Dear Hiring Team,"),
            banned_phrases=(
                "I am excited to apply",
                "I am passionate about",
                "perfect fit",
            ),
            job_text=(
                "Example Industrial Systems GmbH builds offline engineering "
                "software for manufacturing teams. We are hiring a Junior C++ "
                "Software Engineer in Munich. You will develop modern C++ "
                "components for CAD/CAM workflows, process technical drawings, "
                "and integrate computer-vision functions with OpenCV. The role "
                "works with product engineers to test reliable desktop software. "
                "Experience with C++17, geometry, image processing, CMake, and "
                "automated testing is preferred. English is the working language. "
                "The posting does not name a contact person."
            ),
        ),
    )


def _validate_output(
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
    if output.source_hash != sources.load_bundle(case.language).sha256:
        raise RuntimeError(f"{case.language} output has the wrong source hash.")
    if output.cv_version_id != cv_selection.cv_version_id:
        raise RuntimeError(f"{case.language} output has the wrong CV version.")
    if output.cv_reference_hash != cv_selection.cv_reference_hash:
        raise RuntimeError(f"{case.language} output has the wrong CV reference hash.")
    if output.used_previous_cv != cv_selection.used_previous_cv:
        raise RuntimeError(f"{case.language} output has the wrong CV fallback flag.")
    for phrase in case.required_phrases:
        if phrase not in output.letter:
            raise RuntimeError(
                f"{case.language} output is missing a required convention."
            )
    lowered = output.letter.casefold()
    for phrase in case.banned_phrases:
        if phrase.casefold() in lowered:
            raise RuntimeError(f"{case.language} output contains a banned phrase.")
    words = _word_count(output.letter)
    if not _MIN_SMOKE_LETTER_WORDS <= words <= _MAX_SMOKE_LETTER_WORDS:
        raise RuntimeError(
            f"{case.language} output length {words} is outside smoke limits."
        )
    if detect_language(output.letter) != case.language:
        raise RuntimeError(f"{case.language} output failed language detection.")
    parser_warnings = (
        "structured metadata",
        "different language in its metadata",
        "unknown fit-assessment",
    )
    if any(
        marker in note.casefold()
        for marker in parser_warnings
        for note in output.verification_notes
    ):
        raise RuntimeError(f"{case.language} output failed structured parsing.")


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-zÄÖÜäöüß]+(?:[-'][A-Za-zÄÖÜäöüß]+)*", text))


def _parse_language_filter() -> Language | None:
    parser = argparse.ArgumentParser(
        description="Run privacy-safe live generation against fictional data."
    )
    parser.add_argument(
        "--language",
        choices=("de", "en"),
        help="Run only one language; omit to test both.",
    )
    arguments = parser.parse_args()
    return cast(Language | None, arguments.language)


if __name__ == "__main__":
    raise SystemExit(main(_parse_language_filter()))
