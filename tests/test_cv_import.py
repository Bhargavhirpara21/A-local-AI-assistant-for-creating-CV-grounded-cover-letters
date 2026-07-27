"""Tests for the versioned, consent-gated private CV workflow."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import tempfile
import unittest
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from config import build_settings
from core.cv_import import (
    CvConsentRequiredError,
    CvCorruptPendingError,
    CvCorruptVersionError,
    CvExtractionError,
    CvFallbackUnavailableError,
    CvGenerationSelection,
    CvImportWorkflow,
    CvNotReadyError,
    CvPendingExistsError,
    CvPendingStateError,
    CvPublicationError,
    CvReviewRequiredError,
    CvValidationError,
    compute_cv_reference_hash,
)
from llm.base import HealthStatus, LLMResult


_PDF_ONE = b"%PDF-1.7\nsynthetic-one\x00\xff\n%%EOF\n"
_PDF_TWO = b"%PDF-1.4\nsynthetic-two\n%%EOF\n"
_PROFILE_ONE = "# Profile\n\nSynthetic reviewed profile one.\n"
_PROFILE_TWO = "# Profile\n\nSynthetic reviewed profile two.\n"


class _FakeClient:
    """Return queued CV-import results without accessing a model backend."""

    def __init__(self, responses: list[LLMResult] | None = None) -> None:
        self.responses = list(responses or [])
        self.import_paths: list[Path] = []

    def import_cv(self, pdf_path: Path) -> LLMResult:
        """Record the staged path and return the next configured result."""

        self.import_paths.append(pdf_path)
        if not self.responses:
            return LLMResult(text=_PROFILE_ONE)
        return self.responses.pop(0)

    def generate(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        """Reject generation because these tests exercise only CV import."""

        raise AssertionError("Generation is outside the CV workflow test scope.")

    def research_job(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        """Reject research because these tests exercise only CV import."""

        raise AssertionError("Research is outside the CV workflow test scope.")

    def health_check(self) -> HealthStatus:
        """Return a deterministic unused health result."""

        return HealthStatus(ok=True, detail="Synthetic client ready.")


class CvImportWorkflowTests(unittest.TestCase):
    """Prove safe staging, review, activation, fallback, and corruption handling."""

    def setUp(self) -> None:
        """Create isolated private storage and deterministic dependencies."""

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.settings = build_settings(self.root)
        self.client = _FakeClient()
        identifiers: Iterator[str] = iter(
            (
                "a" * 32,
                "b" * 32,
                "c" * 32,
                "d" * 32,
                "e" * 32,
                "f" * 32,
            )
        )
        self.workflow = CvImportWorkflow(
            self.settings,
            self.client,
            clock=lambda: datetime(2026, 7, 27, 9, 30, tzinfo=UTC),
            id_factory=lambda: next(identifiers),
        )

    def tearDown(self) -> None:
        """Release isolated private storage."""

        self._temporary_directory.cleanup()

    def _confirm_version(
        self,
        pdf_bytes: bytes = _PDF_ONE,
        profile: str = _PROFILE_ONE,
        *,
        warnings: tuple[str, ...] = (),
    ) -> str:
        """Import, review, and confirm one synthetic version."""

        self.client.responses.append(LLMResult(text=profile))
        self.workflow.start_import(pdf_bytes, "synthetic.pdf")
        pending = self.workflow.extract_pending()
        self.assertEqual(pending.status, "review")
        version = self.workflow.confirm_pending(
            pending.reference_markdown or "",
            warnings=warnings,
        )
        return version.cv_version_id

    def test_rejects_invalid_pdf_before_staging_or_model_call(self) -> None:
        """Rejected uploads must persist a safe blocker before any model call."""

        cases: tuple[tuple[object, str, str], ...] = (
            (b"%PDF-1.7\n", "resume.txt", "PDF extension"),
            (b"", "resume.pdf", "empty"),
            (b"not-a-pdf", "resume.pdf", "PDF header"),
            (b"%PDF-" + b"x" * 20, "resume.pdf", "25 MiB"),
            ("not bytes", "resume.pdf", "bytes"),
        )

        for payload, filename, message in cases:
            with self.subTest(filename=filename, message=message):
                with tempfile.TemporaryDirectory() as directory:
                    settings = dataclasses.replace(
                        build_settings(Path(directory)),
                        max_cv_pdf_bytes=16,
                    )
                    client = _FakeClient()
                    workflow = CvImportWorkflow(
                        settings,
                        client,
                        id_factory=lambda: "rejected-upload-attempt",
                    )

                    with self.assertRaisesRegex(CvValidationError, message):
                        workflow.start_import(  # type: ignore[arg-type]
                            payload,
                            filename,
                        )

                    pending = workflow.load_pending()
                    self.assertIsNotNone(pending)
                    self.assertEqual(pending.status if pending else None, "failed")
                    self.assertFalse(
                        pending.has_staged_pdf if pending else True
                    )
                    self.assertIsNone(
                        pending.pdf_sha256 if pending else "unexpected"
                    )
                    self.assertEqual(
                        pending.pdf_size_bytes if pending else -1,
                        0,
                    )
                    self.assertEqual(client.import_paths, [])
                    self.assertFalse(
                        (
                            settings.cv_staging_dir
                            / "rejected-upload-attempt"
                        ).exists()
                    )
                    with self.assertRaises(CvConsentRequiredError):
                        workflow.select_for_generation()
                    with self.assertRaises(CvFallbackUnavailableError):
                        workflow.select_for_generation(allow_previous=True)
                    with self.assertRaises(CvPendingStateError):
                        workflow.retry_pending()

    def test_invalid_replacement_requires_consent_before_old_cv_use(self) -> None:
        """A locally rejected replacement must still block silent old-CV use."""

        active_id = self._confirm_version()

        with self.assertRaises(CvValidationError):
            self.workflow.start_import(b"not-a-pdf", "replacement.pdf")

        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()
        fallback = self.workflow.select_for_generation(allow_previous=True)
        self.assertEqual(fallback.cv_version_id, active_id)
        self.assertTrue(fallback.used_previous_cv)
        self.assertTrue(self.workflow.discard_pending())
        selected = self.workflow.select_for_generation()
        self.assertEqual(selected.cv_version_id, active_id)
        self.assertFalse(selected.used_previous_cv)

    def test_staging_failure_keeps_durable_old_cv_consent_gate(self) -> None:
        """A local staging failure must not restore silent old-CV selection."""

        active_id = self._confirm_version()
        original_replace = __import__("os").replace

        def fail_stage_publication(source: Path | str, target: Path | str) -> None:
            """Fail only publication of the newly staged attempt directory."""

            if Path(target).parent == self.settings.cv_staging_dir:
                raise PermissionError("synthetic staging failure")
            original_replace(source, target)

        with patch(
            "core.cv_import.os.replace",
            side_effect=fail_stage_publication,
        ):
            with self.assertRaises(CvPublicationError):
                self.workflow.start_import(_PDF_TWO, "replacement.pdf")

        pending = self.workflow.load_pending()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.status if pending else None, "failed")
        self.assertFalse(pending.has_staged_pdf if pending else True)
        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()
        fallback = self.workflow.select_for_generation(allow_previous=True)
        self.assertEqual(fallback.cv_version_id, active_id)
        self.assertTrue(fallback.used_previous_cv)

    def test_stages_exact_bytes_hash_and_safe_basename_without_calling_model(self) -> None:
        """Starting an import should persist exact bytes and privacy-safe metadata."""

        pending = self.workflow.start_import(
            _PDF_ONE,
            r"C:\private\Résumé<>.PDF",
        )

        expected_hash = hashlib.sha256(_PDF_ONE).hexdigest()
        staged_pdf = self.settings.cv_staging_dir / pending.attempt_id / "cv.pdf"
        self.assertEqual(pending.status, "extracting")
        self.assertEqual(pending.original_name, "Résumé.PDF")
        self.assertEqual(pending.pdf_sha256, expected_hash)
        self.assertEqual(pending.pdf_size_bytes, len(_PDF_ONE))
        self.assertEqual(staged_pdf.read_bytes(), _PDF_ONE)
        self.assertEqual(self.workflow.load_pending(), pending)
        self.assertEqual(self.client.import_paths, [])

    def test_good_extraction_is_canonical_review_state_and_hidden_from_repr(self) -> None:
        """A valid model draft should become UTF-8/LF review text, never active."""

        private_text = "\ufeff# Profile\r\n\r\nSynthetic Ä profile.\r\n"
        self.client.responses.append(LLMResult(text=private_text))
        started = self.workflow.start_import(_PDF_ONE, "cv.pdf")

        pending = self.workflow.extract_pending()

        self.assertEqual(pending.attempt_id, started.attempt_id)
        self.assertEqual(
            pending.reference_markdown,
            "# Profile\n\nSynthetic Ä profile.\n",
        )
        self.assertNotIn("Synthetic Ä profile", repr(pending))
        self.assertIsNone(self.workflow.load_active())
        staged_reference = (
            self.settings.cv_staging_dir / pending.attempt_id / "reference.md"
        )
        self.assertEqual(
            staged_reference.read_bytes(),
            "# Profile\n\nSynthetic Ä profile.\n".encode("utf-8"),
        )

    def test_starting_new_import_blocks_silent_active_selection(self) -> None:
        """Any pending replacement must require per-application fallback consent."""

        active_id = self._confirm_version()
        self.workflow.start_import(_PDF_TWO, "new.pdf")

        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()

        selected = self.workflow.select_for_generation(allow_previous=True)
        self.assertEqual(selected.cv_version_id, active_id)
        self.assertTrue(selected.used_previous_cv)
        self.assertTrue(
            any("previous confirmed CV" in warning for warning in selected.warnings)
        )

    def test_failed_extraction_preserves_active_and_requires_consent(self) -> None:
        """A model failure must retain the old bundle and a durable failed attempt."""

        active_id = self._confirm_version()
        self.client.responses.append(
            LLMResult(
                text="",
                is_error=True,
                error_message="Synthetic extraction outage.",
            )
        )
        self.workflow.start_import(_PDF_TWO, "replacement.pdf")

        with self.assertRaises(CvExtractionError):
            self.workflow.extract_pending()

        pending = self.workflow.load_pending()
        active = self.workflow.load_active()
        self.assertIsNotNone(pending)
        self.assertEqual(pending.status if pending else None, "failed")
        self.assertIsNotNone(pending.error_message if pending else None)
        self.assertEqual(active.cv_version_id if active else None, active_id)
        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()
        selected = self.workflow.select_for_generation(allow_previous=True)
        self.assertEqual(selected.cv_version_id, active_id)
        self.assertTrue(selected.used_previous_cv)

    def test_retry_reuses_staged_pdf_without_reupload(self) -> None:
        """A failed attempt should retry from its exact staged bytes."""

        self.client.responses.extend(
            (
                LLMResult(text="", is_error=True, error_message="Temporary failure."),
                LLMResult(text=_PROFILE_TWO),
            )
        )
        started = self.workflow.start_import(_PDF_TWO, "replacement.pdf")
        with self.assertRaises(CvExtractionError):
            self.workflow.extract_pending()

        retried = self.workflow.retry_pending()

        self.assertEqual(retried.attempt_id, started.attempt_id)
        self.assertEqual(retried.status, "review")
        self.assertEqual(retried.reference_markdown, _PROFILE_TWO)
        self.assertIsNone(retried.error_message)
        self.assertEqual(len(self.client.import_paths), 2)
        self.assertEqual(self.client.import_paths[0], self.client.import_paths[1])
        self.assertEqual(self.client.import_paths[1].read_bytes(), _PDF_TWO)

    def test_invalid_model_markdown_fails_closed_and_can_retry(self) -> None:
        """Non-profile model text must remain an inactive failed attempt."""

        self.client.responses.append(LLMResult(text="# Not a profile\nsecret\n"))
        self.workflow.start_import(_PDF_ONE, "cv.pdf")

        with self.assertRaisesRegex(CvExtractionError, "reviewable"):
            self.workflow.extract_pending()

        pending = self.workflow.load_pending()
        self.assertEqual(pending.status if pending else None, "failed")
        self.assertIsNone(pending.reference_markdown if pending else "unexpected")
        self.assertFalse(
            (
                self.settings.cv_staging_dir
                / (pending.attempt_id if pending else "missing")
                / "reference.md"
            ).exists()
        )

    def test_heading_only_profile_is_not_a_reviewable_cv_reference(self) -> None:
        """A bare heading must fail instead of passing live structural acceptance."""

        self.client.responses.append(LLMResult(text="# Profile\n"))
        self.workflow.start_import(_PDF_ONE, "cv.pdf")

        with self.assertRaisesRegex(CvExtractionError, "reviewable"):
            self.workflow.extract_pending()

        pending = self.workflow.load_pending()
        self.assertEqual(pending.status if pending else None, "failed")
        self.assertIsNone(pending.reference_markdown if pending else "unexpected")

    def test_pdf_mutation_during_model_call_never_creates_review_draft(
        self,
    ) -> None:
        """The exact staged PDF must still match after remote extraction."""

        class MutatingClient(_FakeClient):
            """Simulate an external staged-file change during extraction."""

            def import_cv(self, pdf_path: Path) -> LLMResult:
                """Replace the staged bytes before returning model text."""

                self.import_paths.append(pdf_path)
                pdf_path.write_bytes(_PDF_TWO)
                return LLMResult(text=_PROFILE_ONE)

        client = MutatingClient()
        workflow = CvImportWorkflow(
            self.settings,
            client,
            clock=lambda: datetime(2026, 7, 27, 9, 30, tzinfo=UTC),
            id_factory=lambda: "mutation-attempt",
        )
        workflow.start_import(_PDF_ONE, "cv.pdf")

        with self.assertRaises(CvCorruptPendingError):
            workflow.extract_pending()

        self.assertFalse(
            (
                self.settings.cv_staging_dir
                / "mutation-attempt"
                / "reference.md"
            ).exists()
        )

    def test_review_is_required_before_confirmation(self) -> None:
        """Extracting or failed attempts must never publish as confirmed versions."""

        self.workflow.start_import(_PDF_ONE, "cv.pdf")
        with self.assertRaises(CvReviewRequiredError):
            self.workflow.confirm_pending(_PROFILE_ONE)

        self.client.responses.append(
            LLMResult(text="", is_error=True, error_message="Synthetic failure.")
        )
        with self.assertRaises(CvExtractionError):
            self.workflow.extract_pending()
        with self.assertRaises(CvReviewRequiredError):
            self.workflow.confirm_pending(_PROFILE_ONE)
        self.assertFalse(self.settings.cv_active_path.exists())

    def test_confirmation_publishes_immutable_bundle_and_warning_provenance(self) -> None:
        """Confirmation should atomically select one complete verifiable bundle."""

        warning = "Synthetic conflict requires human verification."
        version_id = self._confirm_version(warnings=(warning,))
        version_dir = self.settings.cv_versions_dir / version_id

        self.assertEqual((version_dir / "cv.pdf").read_bytes(), _PDF_ONE)
        self.assertEqual(
            (version_dir / "reference.md").read_bytes(),
            _PROFILE_ONE.encode("utf-8"),
        )
        metadata = json.loads(
            (version_dir / "metadata.json").read_text(encoding="utf-8")
        )
        pointer = json.loads(
            self.settings.cv_active_path.read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["schema_version"], 1)
        self.assertEqual(metadata["version_id"], version_id)
        self.assertEqual(metadata["warnings"], [warning])
        self.assertEqual(pointer["version_id"], version_id)
        self.assertFalse(self.settings.cv_pending_path.exists())
        self.assertFalse(self.settings.cv_pending_recovery_path.exists())
        self.assertFalse((self.settings.cv_staging_dir / ("a" * 32)).exists())

        selected = self.workflow.select_for_generation()
        self.assertEqual(selected.cv_version_id, version_id)
        self.assertEqual(selected.reference_markdown, _PROFILE_ONE)
        self.assertEqual(
            selected.cv_reference_hash,
            hashlib.sha256(_PROFILE_ONE.encode("utf-8")).hexdigest(),
        )
        self.assertFalse(selected.used_previous_cv)
        self.assertEqual(selected.warnings, (warning,))
        self.assertNotIn("Synthetic reviewed profile", repr(selected))

    def test_confirmed_reimport_keeps_previous_bundle_immutable(self) -> None:
        """Activating a replacement must not edit or remove the prior version."""

        first_id = self._confirm_version()
        first_bytes = (
            self.settings.cv_versions_dir / first_id / "cv.pdf"
        ).read_bytes()
        second_id = self._confirm_version(_PDF_TWO, _PROFILE_TWO)

        self.assertNotEqual(first_id, second_id)
        self.assertEqual(
            (self.settings.cv_versions_dir / first_id / "cv.pdf").read_bytes(),
            first_bytes,
        )
        self.assertEqual(
            self.workflow.select_for_generation().cv_version_id,
            second_id,
        )
        self.assertEqual(
            len(
                tuple(
                    path
                    for path in self.settings.cv_versions_dir.iterdir()
                    if path.is_dir() and not path.name.startswith(".")
                )
            ),
            2,
        )

    def test_active_pointer_failure_preserves_old_active_and_pending_review(self) -> None:
        """A failed pointer replacement must not silently activate a new bundle."""

        old_id = self._confirm_version()
        self.client.responses.append(LLMResult(text=_PROFILE_TWO))
        self.workflow.start_import(_PDF_TWO, "replacement.pdf")
        reviewed = self.workflow.extract_pending()
        original_replace = __import__("os").replace

        def fail_active_pointer(source: Path | str, target: Path | str) -> None:
            """Fail only publication of active.json while allowing bundle publish."""

            if Path(target) == self.settings.cv_active_path:
                raise PermissionError("synthetic pointer lock")
            original_replace(source, target)

        with patch("core.cv_import.os.replace", side_effect=fail_active_pointer):
            with self.assertRaises(CvPublicationError):
                self.workflow.confirm_pending(reviewed.reference_markdown or "")

        active = self.workflow.load_active()
        pending = self.workflow.load_pending()
        self.assertEqual(active.cv_version_id if active else None, old_id)
        self.assertEqual(pending.status if pending else None, "review")
        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()
        self.assertEqual(
            self.workflow.select_for_generation(
                allow_previous=True
            ).cv_version_id,
            old_id,
        )

    def test_pending_cleanup_failure_keeps_fallback_bound_to_prior_version(
        self,
    ) -> None:
        """A cleanup failure must never relabel the newly activated CV as old."""

        old_id = self._confirm_version()
        self.client.responses.append(LLMResult(text=_PROFILE_TWO))
        self.workflow.start_import(_PDF_TWO, "replacement.pdf")
        reviewed = self.workflow.extract_pending()
        original_unlink = Path.unlink

        def fail_pending_unlink(
            path: Path,
            missing_ok: bool = False,
        ) -> None:
            """Fail only removal of the durable pending marker."""

            if path == self.settings.cv_pending_path:
                raise PermissionError("synthetic pending marker lock")
            original_unlink(path, missing_ok=missing_ok)

        with patch.object(
            Path,
            "unlink",
            autospec=True,
            side_effect=fail_pending_unlink,
        ):
            with self.assertRaises(CvPublicationError):
                self.workflow.confirm_pending(reviewed.reference_markdown or "")

        active = self.workflow.load_active()
        self.assertIsNotNone(active)
        self.assertNotEqual(active.cv_version_id if active else None, old_id)
        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()
        fallback = self.workflow.select_for_generation(allow_previous=True)
        self.assertEqual(fallback.cv_version_id, old_id)
        self.assertTrue(fallback.used_previous_cv)

    def test_corrupt_pending_allows_safe_fallback_and_explicit_discard(self) -> None:
        """Corrupt staged data must block silently but remain safely recoverable."""

        old_id = self._confirm_version()
        pending = self.workflow.start_import(_PDF_TWO, "replacement.pdf")
        staged_pdf = (
            self.settings.cv_staging_dir / pending.attempt_id / "cv.pdf"
        )
        staged_pdf.write_bytes(b"%PDF-1.7\ncorrupted after staging\n%%EOF\n")

        with self.assertRaises(CvCorruptPendingError):
            self.workflow.load_pending()
        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()
        fallback = self.workflow.select_for_generation(allow_previous=True)
        self.assertEqual(fallback.cv_version_id, old_id)
        self.assertTrue(fallback.used_previous_cv)

        self.assertTrue(self.workflow.discard_pending())
        self.assertFalse(staged_pdf.parent.exists())
        selected = self.workflow.select_for_generation()
        self.assertEqual(selected.cv_version_id, old_id)
        self.assertFalse(selected.used_previous_cv)

    def test_unreadable_pending_marker_can_be_discarded_with_exact_cleanup(
        self,
    ) -> None:
        """Recovery metadata must remove only its staged private PDF directory."""

        old_id = self._confirm_version()
        pending = self.workflow.start_import(_PDF_TWO, "replacement.pdf")
        staged_directory = (
            self.settings.cv_staging_dir / pending.attempt_id
        )
        unrelated_directory = self.settings.cv_staging_dir / "unrelated-safe-id"
        unrelated_directory.mkdir()
        (unrelated_directory / "keep.txt").write_text(
            "synthetic unrelated file",
            encoding="utf-8",
        )
        self.settings.cv_pending_path.write_text(
            "{not valid json",
            encoding="utf-8",
        )

        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()
        fallback = self.workflow.select_for_generation(allow_previous=True)
        self.assertEqual(fallback.cv_version_id, old_id)
        self.assertTrue(self.workflow.discard_pending())
        self.assertFalse(self.settings.cv_pending_path.exists())
        self.assertFalse(self.settings.cv_pending_recovery_path.exists())
        self.assertFalse(staged_directory.exists())
        self.assertTrue(unrelated_directory.is_dir())
        self.assertEqual(
            self.workflow.select_for_generation().cv_version_id,
            old_id,
        )

    def test_no_previous_active_means_fallback_is_unavailable(self) -> None:
        """Consent cannot create a fallback when no confirmed version exists."""

        with self.assertRaises(CvNotReadyError):
            self.workflow.select_for_generation()

        self.workflow.start_import(_PDF_ONE, "cv.pdf")
        with self.assertRaises(CvConsentRequiredError):
            self.workflow.select_for_generation()
        with self.assertRaises(CvFallbackUnavailableError):
            self.workflow.select_for_generation(allow_previous=True)

    def test_discard_pending_is_explicit_and_restores_normal_active_use(self) -> None:
        """Discarding should remove only the attempt and stop fallback labeling."""

        active_id = self._confirm_version()
        pending = self.workflow.start_import(_PDF_TWO, "replacement.pdf")

        self.assertTrue(self.workflow.discard_pending())
        self.assertFalse(self.workflow.discard_pending())
        self.assertIsNone(self.workflow.load_pending())
        self.assertFalse(
            (self.settings.cv_staging_dir / pending.attempt_id).exists()
        )
        selected = self.workflow.select_for_generation()
        self.assertEqual(selected.cv_version_id, active_id)
        self.assertFalse(selected.used_previous_cv)

    def test_second_upload_cannot_silently_replace_pending_attempt(self) -> None:
        """A pending upload must be discarded before another one is staged."""

        first = self.workflow.start_import(_PDF_ONE, "first.pdf")

        with self.assertRaises(CvPendingExistsError):
            self.workflow.start_import(_PDF_TWO, "second.pdf")

        loaded = self.workflow.load_pending()
        self.assertEqual(loaded.attempt_id if loaded else None, first.attempt_id)
        self.assertEqual(
            (
                self.settings.cv_staging_dir / first.attempt_id / "cv.pdf"
            ).read_bytes(),
            _PDF_ONE,
        )

    def test_corrupt_active_bundle_never_reaches_generation_selection(self) -> None:
        """Mutated PDF, reference, metadata, or pointer data must fail closed."""

        mutations: tuple[str, ...] = (
            "pdf",
            "reference",
            "metadata_schema",
            "pointer_schema",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    settings = build_settings(Path(directory))
                    client = _FakeClient([LLMResult(text=_PROFILE_ONE)])
                    identifiers = iter(("1" * 32, "2" * 32))
                    workflow = CvImportWorkflow(
                        settings,
                        client,
                        id_factory=lambda: next(identifiers),
                    )
                    workflow.start_import(_PDF_ONE, "cv.pdf")
                    reviewed = workflow.extract_pending()
                    version = workflow.confirm_pending(
                        reviewed.reference_markdown or ""
                    )
                    version_dir = settings.cv_versions_dir / version.cv_version_id
                    if mutation == "pdf":
                        (version_dir / "cv.pdf").write_bytes(_PDF_TWO)
                    elif mutation == "reference":
                        (version_dir / "reference.md").write_text(
                            _PROFILE_TWO,
                            encoding="utf-8",
                            newline="\n",
                        )
                    elif mutation == "metadata_schema":
                        metadata_path = version_dir / "metadata.json"
                        metadata = json.loads(
                            metadata_path.read_text(encoding="utf-8")
                        )
                        metadata["schema_version"] = 999
                        metadata_path.write_text(
                            json.dumps(metadata),
                            encoding="utf-8",
                            newline="\n",
                        )
                    else:
                        pointer = json.loads(
                            settings.cv_active_path.read_text(encoding="utf-8")
                        )
                        pointer["schema_version"] = 999
                        settings.cv_active_path.write_text(
                            json.dumps(pointer),
                            encoding="utf-8",
                            newline="\n",
                        )

                    with self.assertRaises(CvCorruptVersionError):
                        workflow.select_for_generation()

    def test_selection_constructor_validates_content_and_exact_hash(self) -> None:
        """Manually created selections must enforce the same prompt-safety invariants."""

        digest = compute_cv_reference_hash(_PROFILE_ONE)
        selection = CvGenerationSelection(
            cv_version_id="a" * 32,
            reference_markdown=_PROFILE_ONE,
            cv_reference_hash=digest,
            used_previous_cv=False,
            warnings=("Synthetic warning.",),
        )
        self.assertEqual(selection.cv_reference_hash, digest)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            selection.cv_version_id = "b" * 32  # type: ignore[misc]

        invalid_cases: tuple[dict[str, object], ...] = (
            {"cv_version_id": "../unsafe"},
            {"reference_markdown": "# Wrong\n"},
            {"cv_reference_hash": "A" * 64},
            {"cv_reference_hash": "0" * 64},
            {"used_previous_cv": 1},
            {"warnings": ["not", "a", "tuple"]},
        )
        base: dict[str, object] = {
            "cv_version_id": selection.cv_version_id,
            "reference_markdown": selection.reference_markdown,
            "cv_reference_hash": selection.cv_reference_hash,
            "used_previous_cv": selection.used_previous_cv,
            "warnings": selection.warnings,
        }
        for changes in invalid_cases:
            with self.subTest(changes=changes):
                with self.assertRaises(CvValidationError):
                    CvGenerationSelection(**(base | changes))  # type: ignore[arg-type]

    def test_warnings_unicode_and_hashes_round_trip_without_privacy_logs(self) -> None:
        """Persistence should preserve warnings while operational logs omit secrets."""

        secret_name = "SECRET-NAME.pdf"
        secret_content = "# Profile\n\nSECRET-CONTENT-Ä.\n"
        secret_hash = hashlib.sha256(_PDF_ONE).hexdigest()
        secret_path = str(self.settings.cv_dir)
        self.client.responses.append(
            LLMResult(
                text="",
                is_error=True,
                error_message="SECRET-PROVIDER-MESSAGE",
            )
        )

        with self.assertLogs("core.cv_import", level=logging.INFO) as captured:
            self.workflow.start_import(_PDF_ONE, secret_name)
            with self.assertRaises(CvExtractionError):
                self.workflow.extract_pending()

        logs = "\n".join(captured.output)
        for secret in (
            secret_name,
            secret_content,
            secret_hash,
            secret_path,
            "SECRET-PROVIDER-MESSAGE",
        ):
            self.assertNotIn(secret, logs)


if __name__ == "__main__":
    unittest.main()
