"""Tests for private, collision-safe letter and generation-trace archives."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from config import Settings, build_settings
from core.archive import (
    ArchiveError,
    LetterArchive,
    sanitize_filename_component,
    save_letter,
)


@dataclass(frozen=True, slots=True)
class _GenerationTraceStub:
    """Supply the generator trace fields consumed by the archive boundary."""

    operation: str
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
class _LetterOutputStub:
    """Supply the generator output fields consumed by the archive boundary."""

    letter: str
    company: str
    role: str
    language: str
    contact_person: str | None
    reference_number: str | None
    location: str | None
    job_url: str | None
    fit_assessment: str
    fit_rationale: str
    verification_notes: tuple[str, ...]
    research_urls: tuple[str, ...]
    source_hash: str
    cv_version_id: str
    cv_reference_hash: str
    used_previous_cv: bool
    input_hash: str
    trace: _GenerationTraceStub | None


def _generation_input_hash(trace: _GenerationTraceStub) -> str:
    """Reproduce the versioned framed hash contract independently in tests."""

    digest = hashlib.sha256()
    digest.update(b"AutoCover.GenerationTrace.v3\0")
    fields: tuple[tuple[str, str | None], ...] = (
        ("operation", trace.operation),
        ("backend", trace.backend),
        ("model", trace.model),
        ("source_hash", trace.source_hash),
        ("cv_version_id", trace.cv_version_id),
        ("cv_reference_hash", trace.cv_reference_hash),
        (
            "used_previous_cv",
            "true" if trace.used_previous_cv else "false",
        ),
        ("system_prompt", trace.system_prompt),
        ("user_prompt", trace.user_prompt),
    )
    for name, value in fields:
        encoded_name = name.encode("utf-8")
        encoded_value = (
            b"\x00" if value is None else b"\x01" + value.encode("utf-8")
        )
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(encoded_value).to_bytes(8, "big"))
        digest.update(encoded_value)
    return digest.hexdigest()


class LetterArchiveTests(unittest.TestCase):
    """Verify metadata, provenance, filenames, and transactional persistence."""

    _temporary_directory: tempfile.TemporaryDirectory[str]
    root: Path
    settings: Settings
    now: datetime
    trace: _GenerationTraceStub
    output: _LetterOutputStub

    def setUp(self) -> None:
        """Create isolated settings and a deterministic fictional fixture."""

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.settings = build_settings(self.root)
        self.now = datetime(2026, 7, 24, 15, 6, tzinfo=UTC)
        incomplete_trace = _GenerationTraceStub(
            operation="generation",
            backend="agent_sdk",
            model=None,
            source_hash="a" * 64,
            cv_version_id="cv-fictional-v1",
            cv_reference_hash="b" * 64,
            used_previous_cv=False,
            system_prompt="# Fictional system prompt\n\nUse only supplied facts.\r\n",
            user_prompt=(
                "# FICTIONAL JOB DESCRIPTION\r\n"
                "Northstar Robotics needs a control systems engineer.\r\n"
            ),
            input_hash="",
        )
        self.trace = replace(
            incomplete_trace,
            input_hash=_generation_input_hash(incomplete_trace),
        )
        self.output = _LetterOutputStub(
            letter=(
                "Application for Control Systems Engineer\n\n"
                "Dear Hiring Team,\n\n"
                "I build reliable fictional control-system test fixtures.\n\n"
                "Kind regards,\nTaylor Example"
            ),
            company="Northstar/Robotics GmbH",
            role="Control Systems Engineer",
            language="en",
            contact_person=None,
            reference_number="REF-42",
            location="München",
            job_url="https://northstar.example/jobs/42?lang=en",
            fit_assessment="Strong match",
            fit_rationale="The fictional role matches the supplied test evidence.",
            verification_notes=(
                "Confirm the fictional start date.",
                "Confirm the fictional start date.",
                "No certification was claimed.",
            ),
            research_urls=(
                "https://northstar.example/about",
                "https://northstar.example/about",
            ),
            source_hash="a" * 64,
            cv_version_id=self.trace.cv_version_id,
            cv_reference_hash=self.trace.cv_reference_hash,
            used_previous_cv=self.trace.used_previous_cv,
            input_hash=self.trace.input_hash,
            trace=self.trace,
        )

    def tearDown(self) -> None:
        """Release the isolated archive directory."""

        self._temporary_directory.cleanup()

    def test_saves_complete_frontmatter_and_exact_private_trace(self) -> None:
        """One save should retain output metadata and exact generation inputs."""

        job_text = (
            "We need IEC 61131-3 experience.\r\n"
            "Location: München.\r\n"
            "Untrusted markup such as <tag> and ``` remains source text."
        )
        research_urls = (
            " https://northstar.example/about ",
            "https://northstar.example/careers?q=controls",
            "",
        )

        path = save_letter(
            self.output,
            job_text,
            "application-001",
            self.settings,
            research_urls=research_urls,
            clock=lambda: self.now,
        )

        self.assertEqual(
            path.name,
            "2026-07-24_NorthstarRobotics-GmbH_Control-Systems-Engineer_1506.md",
        )
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        self.assertEqual(raw, text.encode("utf-8"))
        self.assertTrue(text.startswith("---\n"))
        self.assertIn('application_id: "application-001"\n', text)
        self.assertIn('company: "Northstar/Robotics GmbH"\n', text)
        self.assertIn('location: "München"\n', text)
        self.assertIn("contact_person: null\n", text)
        self.assertIn("refined: false\n", text)
        self.assertIn(
            'fit_rationale: "The fictional role matches the supplied test evidence."\n',
            text,
        )
        self.assertIn("verification_notes:\n", text)
        self.assertEqual(
            text.count('  - "Confirm the fictional start date."\n'),
            1,
        )
        self.assertIn('  - "No certification was claimed."\n', text)
        self.assertIn(f'source_hash: "{"a" * 64}"\n', text)
        self.assertIn(f'input_hash: "{self.output.input_hash}"\n', text)
        self.assertIn('cv_version_id: "cv-fictional-v1"\n', text)
        self.assertIn(f'cv_reference_hash: "{"b" * 64}"\n', text)
        self.assertIn("used_previous_cv: false\n", text)
        self.assertEqual(
            text.count('  - "https://northstar.example/about"\n'),
            1,
        )
        self.assertIn(
            '  - "https://northstar.example/careers?q=controls"\n',
            text,
        )
        self.assertIn("\n---\n\n" + self.output.letter + "\n\n", text)
        canonical_job_text = job_text.replace("\r\n", "\n")
        self.assertIn(
            "<details><summary>Job description used</summary>\n\n"
            + canonical_job_text
            + "\n\n</details>\n",
            text,
        )

        trace_path = path.with_suffix(".trace.json")
        self.assertTrue(trace_path.is_file())
        self.assertEqual(
            json.loads(trace_path.read_text(encoding="utf-8")),
            {
                "operation": "generation",
                "backend": "agent_sdk",
                "model": None,
                "system_prompt": self.trace.system_prompt,
                "user_prompt": self.trace.user_prompt,
                "source_hash": self.output.source_hash,
                "cv_version_id": self.output.cv_version_id,
                "cv_reference_hash": self.output.cv_reference_hash,
                "used_previous_cv": self.output.used_previous_cv,
                "input_hash": self.output.input_hash,
            },
        )
        self.assertTrue(trace_path.read_bytes().endswith(b"\n"))

    def test_previous_cv_consent_is_hash_bound_and_archived_as_true(self) -> None:
        """Explicit previous-CV use must survive in both private artifacts."""

        incomplete_trace = replace(
            self.trace,
            used_previous_cv=True,
            input_hash="",
        )
        trace = replace(
            incomplete_trace,
            input_hash=_generation_input_hash(incomplete_trace),
        )
        output = replace(
            self.output,
            used_previous_cv=True,
            input_hash=trace.input_hash,
            trace=trace,
        )

        path = LetterArchive(self.settings, clock=lambda: self.now).save_letter(
            output,
            "Complete fictional posting",
            "application-previous-cv",
        )

        text = path.read_text(encoding="utf-8")
        trace_data = json.loads(
            path.with_suffix(".trace.json").read_text(encoding="utf-8")
        )
        self.assertIn("used_previous_cv: true\n", text)
        self.assertIs(trace_data["used_previous_cv"], True)

    def test_frontmatter_values_cannot_inject_new_yaml_keys(self) -> None:
        """Model-derived strings should remain safely quoted YAML scalars."""

        hostile = replace(
            self.output,
            company='Example Labs\nrefined: true\n"quoted"',
            fit_rationale="Reason\nsource_hash: forged",
            verification_notes=("Review\ninput_hash: forged",),
            research_urls=("https://safe.example/reference",),
        )

        path = LetterArchive(self.settings, clock=lambda: self.now).save_letter(
            hostile,
            "Complete fictional posting",
            "application-002",
        )
        frontmatter = path.read_text(encoding="utf-8").split("---\n", maxsplit=2)[1]
        company_line = next(
            line for line in frontmatter.splitlines() if line.startswith("company: ")
        )
        company_value = json.loads(company_line.removeprefix("company: "))

        self.assertEqual(company_value, hostile.company)
        self.assertNotIn("\nrefined: true\n", frontmatter)
        self.assertNotIn("\nsource_hash: forged\n", frontmatter)
        self.assertNotIn("\ninput_hash: forged\n", frontmatter)

    def test_rejects_invalid_research_urls_before_writing(self) -> None:
        """Only validated absolute HTTP(S) research provenance may be archived."""

        invalid_groups = (
            ("https://safe.example/\nsource_hash: forged",),
            ("file:///C:/private.txt",),
            ("https://user:password@example.com/job",),
            ("not-a-url",),
        )
        archive = LetterArchive(self.settings, clock=lambda: self.now)
        for research_urls in invalid_groups:
            with self.subTest(research_urls=research_urls):
                with self.assertRaisesRegex(ArchiveError, "valid absolute HTTP"):
                    archive.save_letter(
                        replace(self.output, research_urls=research_urls),
                        "Complete fictional posting",
                        "application-url-check",
                    )

        self.assertFalse(self.settings.letters_dir.exists())

    def test_refined_name_and_pair_collisions_never_overwrite(self) -> None:
        """Repeated saves should publish matched letter/trace pairs with suffixes."""

        archive = LetterArchive(self.settings, clock=lambda: self.now)
        first = archive.save_letter(
            self.output,
            "Fictional posting one",
            "application-003",
        )
        first_letter = first.read_text(encoding="utf-8")
        first_trace = first.with_suffix(".trace.json").read_text(encoding="utf-8")

        second = archive.save_letter(
            self.output,
            "Fictional posting two",
            "application-004",
        )
        incomplete_refined_trace = replace(
            self.trace,
            operation="refinement",
            input_hash="",
        )
        refined_trace = replace(
            incomplete_refined_trace,
            input_hash=_generation_input_hash(incomplete_refined_trace),
        )
        refined_output = replace(
            self.output,
            input_hash=refined_trace.input_hash,
            trace=refined_trace,
        )
        refined = archive.save_letter(
            refined_output,
            "Fictional posting three",
            "application-003",
            refined=True,
        )

        self.assertEqual(
            second.name,
            (
                "2026-07-24_NorthstarRobotics-GmbH_"
                "Control-Systems-Engineer_1506_2.md"
            ),
        )
        self.assertEqual(
            refined.name,
            (
                "2026-07-24_NorthstarRobotics-GmbH_"
                "Control-Systems-Engineer_refined_1506.md"
            ),
        )
        self.assertTrue(second.with_suffix(".trace.json").is_file())
        self.assertTrue(refined.with_suffix(".trace.json").is_file())
        self.assertEqual(first.read_text(encoding="utf-8"), first_letter)
        self.assertEqual(
            first.with_suffix(".trace.json").read_text(encoding="utf-8"),
            first_trace,
        )
        self.assertIn(
            "Fictional posting two",
            second.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "refined: true",
            refined.read_text(encoding="utf-8"),
        )

    def test_an_existing_trace_alone_forces_a_collision_suffix(self) -> None:
        """A trace orphan must not be overwritten by a later archive pair."""

        self.settings.letters_dir.mkdir(parents=True)
        original_trace = self.settings.letters_dir / (
            "2026-07-24_NorthstarRobotics-GmbH_"
            "Control-Systems-Engineer_1506.trace.json"
        )
        original_trace.write_text("reserved trace\n", encoding="utf-8")

        path = LetterArchive(self.settings, clock=lambda: self.now).save_letter(
            self.output,
            "Complete fictional posting",
            "application-005",
        )

        self.assertTrue(path.name.endswith("_1506_2.md"))
        self.assertEqual(
            original_trace.read_text(encoding="utf-8"),
            "reserved trace\n",
        )
        self.assertTrue(path.with_suffix(".trace.json").is_file())

    def test_windows_filename_component_strips_every_unicode_cc_control(self) -> None:
        """Windows-invalid, reserved, trailing, long, and Cc text should be safe."""

        cases = {
            "Northstar/Robotics GmbH": "NorthstarRobotics-GmbH",
            "  Jörg   Müller  ": "Jörg-Müller",
            "CON": "_CON",
            "nul.txt": "_nul.txt",
            "Report.\x07 ": "Report",
            "Alpha\u0085Beta\u009fGamma": "AlphaBetaGamma",
            '<>:"/\\|?*\x00': "Unknown",
            "normal-name": "normal-name",
        }
        for original, expected in cases.items():
            with self.subTest(original=original):
                self.assertEqual(sanitize_filename_component(original), expected)

        long_value = sanitize_filename_component("ä" * 80)
        self.assertEqual(len(long_value), 60)
        self.assertEqual(long_value, "ä" * 60)
        self.assertFalse(long_value.endswith((" ", ".")))

    def test_second_publication_failure_rolls_back_both_files(self) -> None:
        """Failure after publishing one artifact should leave no partial pair."""

        archive = LetterArchive(self.settings, clock=lambda: self.now)
        real_link = os.link
        link_count = 0

        def fail_second_link(source: Path, destination: Path) -> None:
            nonlocal link_count
            link_count += 1
            if link_count == 2:
                raise PermissionError("simulated second publication failure")
            real_link(source, destination)

        with mock.patch("core.archive.os.link", side_effect=fail_second_link):
            with self.assertLogs("core.archive", level="ERROR"):
                with self.assertRaisesRegex(ArchiveError, "Could not archive"):
                    archive.save_letter(
                        self.output,
                        "Complete fictional posting",
                        "application-006",
                    )

        self.assertEqual(
            tuple(self.settings.letters_dir.glob("*")),
            (),
        )

    def test_rejects_missing_malformed_or_inconsistent_provenance(self) -> None:
        """Trace and SHA-256 values must be complete and mutually consistent."""

        cases: tuple[tuple[str, _LetterOutputStub, bool], ...] = (
            ("missing trace", replace(self.output, trace=None), False),
            ("blank input hash", replace(self.output, input_hash=""), False),
            (
                "malformed input hash",
                replace(
                    self.output,
                    input_hash="not-a-sha256",
                    trace=replace(self.trace, input_hash="not-a-sha256"),
                ),
                False,
            ),
            (
                "mismatched input hash",
                replace(
                    self.output,
                    trace=replace(self.trace, input_hash="c" * 64),
                ),
                False,
            ),
            (
                "well-formed but unverified input hash",
                replace(
                    self.output,
                    input_hash="c" * 64,
                    trace=replace(self.trace, input_hash="c" * 64),
                ),
                False,
            ),
            (
                "malformed source hash",
                replace(self.output, source_hash="short"),
                False,
            ),
            (
                "well-formed mismatched source hash",
                self._output_with_trace_source_hash("b" * 64),
                False,
            ),
            (
                "blank output CV version",
                replace(self.output, cv_version_id=""),
                False,
            ),
            (
                "blank trace CV version",
                replace(
                    self.output,
                    trace=replace(self.trace, cv_version_id=""),
                ),
                False,
            ),
            (
                "mismatched CV version",
                replace(
                    self.output,
                    trace=replace(self.trace, cv_version_id="cv-other-v2"),
                ),
                False,
            ),
            (
                "malformed output CV reference hash",
                replace(self.output, cv_reference_hash="short"),
                False,
            ),
            (
                "mismatched CV reference hash",
                replace(
                    self.output,
                    trace=replace(self.trace, cv_reference_hash="c" * 64),
                ),
                False,
            ),
            (
                "non-boolean output previous-CV flag",
                replace(self.output, used_previous_cv="false"),
                False,
            ),
            (
                "non-boolean trace previous-CV flag",
                replace(
                    self.output,
                    trace=replace(self.trace, used_previous_cv="false"),
                ),
                False,
            ),
            (
                "mismatched previous-CV flag",
                replace(
                    self.output,
                    trace=replace(self.trace, used_previous_cv=True),
                ),
                False,
            ),
            (
                "previous-CV flag tampered without rehash",
                replace(
                    self.output,
                    used_previous_cv=True,
                    trace=replace(self.trace, used_previous_cv=True),
                ),
                False,
            ),
            (
                "blank system prompt",
                replace(
                    self.output,
                    trace=replace(self.trace, system_prompt=""),
                ),
                False,
            ),
            (
                "wrong operation",
                replace(
                    self.output,
                    trace=replace(self.trace, operation="refinement"),
                ),
                False,
            ),
        )
        archive = LetterArchive(self.settings, clock=lambda: self.now)
        for label, output, refined in cases:
            with self.subTest(label=label):
                with self.assertRaises(ArchiveError):
                    archive.save_letter(
                        output,
                        "Complete fictional posting",
                        "application-007",
                        refined=refined,
                    )

        self.assertFalse(self.settings.letters_dir.exists())

    def _output_with_trace_source_hash(
        self,
        source_hash: str,
    ) -> _LetterOutputStub:
        """Return an output whose internally valid trace names another source."""

        incomplete_trace = replace(
            self.trace,
            source_hash=source_hash,
            input_hash="",
        )
        trace = replace(
            incomplete_trace,
            input_hash=_generation_input_hash(incomplete_trace),
        )
        return replace(
            self.output,
            input_hash=trace.input_hash,
            trace=trace,
        )

    def test_rejects_empty_required_archive_content_and_naive_clock(self) -> None:
        """Empty identifiers/content and ambiguous timestamps must fail closed."""

        archive = LetterArchive(self.settings, clock=lambda: self.now)
        with self.assertRaises(ArchiveError):
            archive.save_letter(self.output, "", "application-008")
        with self.assertRaises(ArchiveError):
            archive.save_letter(self.output, "Posting", " ")

        with self.assertRaises(ArchiveError):
            archive.save_letter(
                replace(self.output, letter=" \n"),
                "Posting",
                "application-009",
            )

        naive_archive = LetterArchive(
            self.settings,
            clock=lambda: datetime(2026, 7, 24, 15, 6),
        )
        with self.assertRaisesRegex(ArchiveError, "timezone-aware"):
            naive_archive.save_letter(
                self.output,
                "Posting",
                "application-010",
            )


if __name__ == "__main__":
    unittest.main()
