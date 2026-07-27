"""Tests for the consent-gated live CV-import acceptance command."""

from __future__ import annotations

import dataclasses
import logging
import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import Settings, build_settings
from core.cv_import import CvImportWorkflow
from core.source_library import Language
from llm.base import HealthStatus, LLMClient, LLMResult
from scripts.live_cv_import_acceptance import (
    CvAcceptanceResult,
    CvAcceptanceConsentError,
    CvAcceptanceEnvironmentError,
    CvAcceptanceSourceChangedError,
    CvAcceptanceWorkflowError,
    CvExternalPdfAccessError,
    _read_external_pdf,
    run_acceptance,
)

_PDF = b"%PDF-1.7\nsynthetic acceptance PDF\n%%EOF\n"
_PROFILE = "# Profile\n\nSynthetic acceptance reference.\n"
_DE_HASH = "a" * 64
_EN_HASH = "b" * 64
_CHANGED_DE_HASH = "c" * 64


class _FakeClient:
    """Record CV imports while rejecting every unrelated model operation."""

    def __init__(self, response: LLMResult | None = None) -> None:
        self.response = response or LLMResult(text=_PROFILE)
        self.import_paths: list[Path] = []
        self.health_calls = 0

    def import_cv(self, pdf_path: Path) -> LLMResult:
        """Return one configured extraction result."""

        self.import_paths.append(pdf_path)
        return self.response

    def generate(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        """Reject generation because acceptance exercises only CV import."""

        raise AssertionError("Generation must not run during CV acceptance.")

    def research_job(
        self,
        system: str,
        prompt: str,
        *,
        model: str | None = None,
    ) -> LLMResult:
        """Reject research because acceptance exercises only CV import."""

        raise AssertionError("Research must not run during CV acceptance.")

    def health_check(self) -> HealthStatus:
        """Fail if the command spends a second remote call on a health probe."""

        self.health_calls += 1
        raise AssertionError("CV acceptance must not run a health probe.")


class _FakeSources:
    """Return deterministic local bundle hashes without exposing source text."""

    def __init__(
        self,
        snapshots: tuple[tuple[str, str], ...] = ((_DE_HASH, _EN_HASH),),
        *,
        ready: bool = True,
    ) -> None:
        self.snapshots = snapshots
        self.ready = ready
        self.load_calls: list[Language] = []

    def is_ready(self) -> bool:
        """Return the configured readiness state."""

        return self.ready

    def load_bundle(self, language: Language) -> SimpleNamespace:
        """Return only the hash needed by the acceptance command."""

        snapshot_index = min(len(self.load_calls) // 2, len(self.snapshots) - 1)
        self.load_calls.append(language)
        snapshot = self.snapshots[snapshot_index]
        digest = snapshot[0] if language == "de" else snapshot[1]
        return SimpleNamespace(sha256=digest)


class _ExplodingEnvironment(Mapping[str, str]):
    """Prove that even environment access happens only after consent."""

    def __getitem__(self, key: str) -> str:
        raise AssertionError(f"Environment was accessed through {key}.")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("Environment was iterated.")

    def __len__(self) -> int:
        raise AssertionError("Environment length was inspected.")

    def get(self, key: str, default: str | None = None) -> str | None:
        raise AssertionError(f"Environment was accessed through {key}.")


class LiveCvImportAcceptanceTests(unittest.TestCase):
    """Prove privacy, single-call behavior, and review-only persistence."""

    def setUp(self) -> None:
        """Create isolated public test inputs and private application storage."""

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.settings = build_settings(self.root / "project")
        self.external_pdf = self.root / "external secret CV.pdf"
        self.external_pdf.write_bytes(_PDF)

    def tearDown(self) -> None:
        """Release all synthetic acceptance files."""

        self._temporary_directory.cleanup()

    def _workflow_builder(
        self,
        holder: dict[str, CvImportWorkflow],
    ) -> Callable[[Settings, LLMClient], CvImportWorkflow]:
        """Build a deterministic real workflow and expose it to assertions."""

        def build(settings: Settings, client: LLMClient) -> CvImportWorkflow:
            workflow = CvImportWorkflow(
                settings,
                client,
                clock=lambda: datetime(2026, 7, 27, 12, 0, tzinfo=UTC),
                id_factory=lambda: "acceptance-attempt",
            )
            holder["workflow"] = workflow
            return workflow

        return build

    def _run(
        self,
        *,
        client: _FakeClient | None = None,
        sources: _FakeSources | None = None,
        settings: Settings | None = None,
    ) -> tuple[CvAcceptanceResult, _FakeClient, CvImportWorkflow]:
        """Run one successful synthetic acceptance with explicit dependencies."""

        selected_client = client or _FakeClient()
        selected_sources = sources or _FakeSources()
        selected_settings = settings or self.settings
        holder: dict[str, CvImportWorkflow] = {}
        result = run_acceptance(
            self.external_pdf,
            confirmed=True,
            environment={},
            settings_builder=lambda: selected_settings,
            client_builder=lambda value: selected_client,
            source_builder=lambda value: selected_sources,
            workflow_builder=self._workflow_builder(holder),
        )
        return result, selected_client, holder["workflow"]

    def test_consent_is_checked_before_environment_path_or_settings_access(self) -> None:
        """Refusal must happen before any private or machine-state inspection."""

        accesses: list[str] = []

        def settings_builder() -> Settings:
            accesses.append("settings")
            return self.settings

        def pdf_reader(path: Path, maximum_bytes: int) -> bytes:
            del path, maximum_bytes
            accesses.append("path")
            return _PDF

        with self.assertRaises(CvAcceptanceConsentError):
            run_acceptance(
                self.external_pdf,
                confirmed=False,
                environment=_ExplodingEnvironment(),
                settings_builder=settings_builder,
                pdf_reader=pdf_reader,
            )

        self.assertEqual(accesses, [])

    def test_backend_and_api_key_guards_run_before_private_file_access(self) -> None:
        """Only subscription-backed Agent SDK mode may perform this live gate."""

        path_reads: list[Path] = []

        def pdf_reader(path: Path, maximum_bytes: int) -> bytes:
            del maximum_bytes
            path_reads.append(path)
            return _PDF

        api_settings = dataclasses.replace(
            self.settings,
            backend="anthropic_api",
        )
        with self.assertRaisesRegex(
            CvAcceptanceEnvironmentError,
            "Agent SDK",
        ):
            run_acceptance(
                self.external_pdf,
                confirmed=True,
                environment={},
                settings_builder=lambda: api_settings,
                pdf_reader=pdf_reader,
            )
        with self.assertRaisesRegex(
            CvAcceptanceEnvironmentError,
            "ANTHROPIC_API_KEY",
        ):
            run_acceptance(
                self.external_pdf,
                confirmed=True,
                environment={"ANTHROPIC_API_KEY": "secret-key"},
                settings_builder=lambda: self.settings,
                pdf_reader=pdf_reader,
            )

        self.assertEqual(path_reads, [])

    def test_success_uses_one_import_and_leaves_only_review_pending(self) -> None:
        """Acceptance must stage once, extract once, and never activate a CV."""

        result, client, workflow = self._run()
        pending = workflow.load_pending()

        self.assertEqual(len(client.import_paths), 1)
        self.assertEqual(client.health_calls, 0)
        self.assertIsNotNone(pending)
        self.assertEqual(pending.status if pending else None, "review")
        self.assertEqual(pending.reference_markdown if pending else None, _PROFILE)
        self.assertIsNone(workflow.load_active())
        self.assertFalse(self.settings.cv_active_path.exists())
        self.assertEqual(result.attempt_id, "acceptance-attempt")
        self.assertEqual(result.pending_status, "review")
        self.assertTrue(result.source_hashes_unchanged)
        self.assertEqual(
            client.import_paths[0].read_bytes(),
            self.external_pdf.read_bytes(),
        )

    def test_extraction_failure_is_not_retried_and_sources_are_rechecked(self) -> None:
        """A failed remote call should remain one failed pending attempt."""

        client = _FakeClient(
            LLMResult(
                text="",
                is_error=True,
                error_message="SECRET provider detail",
            )
        )
        sources = _FakeSources()
        holder: dict[str, CvImportWorkflow] = {}

        with self.assertRaises(CvAcceptanceWorkflowError):
            run_acceptance(
                self.external_pdf,
                confirmed=True,
                environment={},
                settings_builder=lambda: self.settings,
                client_builder=lambda value: client,
                source_builder=lambda value: sources,
                workflow_builder=self._workflow_builder(holder),
            )

        pending = holder["workflow"].load_pending()
        self.assertEqual(len(client.import_paths), 1)
        self.assertEqual(pending.status if pending else None, "failed")
        self.assertEqual(sources.load_calls, ["de", "en", "de", "en"])
        self.assertFalse(self.settings.cv_active_path.exists())

    def test_source_change_fails_gate_but_keeps_review_draft_inactive(self) -> None:
        """Concurrent source edits must fail acceptance without losing the draft."""

        sources = _FakeSources(
            snapshots=(
                (_DE_HASH, _EN_HASH),
                (_CHANGED_DE_HASH, _EN_HASH),
            )
        )
        client = _FakeClient()
        holder: dict[str, CvImportWorkflow] = {}

        with self.assertRaises(CvAcceptanceSourceChangedError):
            run_acceptance(
                self.external_pdf,
                confirmed=True,
                environment={},
                settings_builder=lambda: self.settings,
                client_builder=lambda value: client,
                source_builder=lambda value: sources,
                workflow_builder=self._workflow_builder(holder),
            )

        pending = holder["workflow"].load_pending()
        self.assertEqual(len(client.import_paths), 1)
        self.assertEqual(pending.status if pending else None, "review")
        self.assertFalse(self.settings.cv_active_path.exists())

    def test_missing_source_library_blocks_before_pdf_or_client_access(self) -> None:
        """The acceptance comparison requires a complete managed source library."""

        accesses: list[str] = []
        sources = _FakeSources(ready=False)

        def pdf_reader(path: Path, maximum_bytes: int) -> bytes:
            del path, maximum_bytes
            accesses.append("path")
            return _PDF

        def client_builder(settings: Settings) -> _FakeClient:
            del settings
            accesses.append("client")
            return _FakeClient()

        with self.assertRaises(CvAcceptanceWorkflowError):
            run_acceptance(
                self.external_pdf,
                confirmed=True,
                environment={},
                settings_builder=lambda: self.settings,
                client_builder=client_builder,
                source_builder=lambda value: sources,
                pdf_reader=pdf_reader,
            )

        self.assertEqual(accesses, [])

    def test_windows_file_errors_are_actionable_and_path_free(self) -> None:
        """ACL, lock, and OneDrive failures should have distinct safe guidance."""

        cases: tuple[tuple[int, str], ...] = (
            (5, "permission"),
            (32, "Close"),
            (33, "Close"),
            (362, "offline"),
        )
        for winerror, expected in cases:
            with self.subTest(winerror=winerror):
                error = OSError(f"SECRET OS detail at {self.external_pdf}")
                error.winerror = winerror  # type: ignore[attr-defined]
                with patch.object(Path, "open", side_effect=error):
                    with self.assertRaises(CvExternalPdfAccessError) as raised:
                        _read_external_pdf(
                            self.external_pdf,
                            self.settings.max_cv_pdf_bytes,
                        )
                message = str(raised.exception)
                self.assertIn(expected, message)
                self.assertNotIn(str(self.external_pdf), message)
                self.assertNotIn("SECRET OS detail", message)

    def test_external_reader_enforces_size_without_logging_path(self) -> None:
        """The local reader should stop at the configured byte ceiling."""

        with self.assertRaisesRegex(
            CvExternalPdfAccessError,
            "size limit",
        ) as raised:
            _read_external_pdf(self.external_pdf, len(_PDF) - 1)

        self.assertNotIn(str(self.external_pdf), str(raised.exception))

    def test_success_logs_no_paths_content_or_hash_values(self) -> None:
        """Operational logs and result repr must contain no private material."""

        sources = _FakeSources()
        with self.assertLogs(
            "scripts.live_cv_import_acceptance",
            level=logging.INFO,
        ) as captured:
            result, _, _ = self._run(sources=sources)

        logs = "\n".join(captured.output)
        for secret in (
            str(self.external_pdf),
            self.external_pdf.name,
            _PROFILE,
            _DE_HASH,
            _EN_HASH,
        ):
            self.assertNotIn(secret, logs)
            self.assertNotIn(secret, repr(result))


if __name__ == "__main__":
    unittest.main()
