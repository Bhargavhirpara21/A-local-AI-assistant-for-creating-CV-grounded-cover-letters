"""Tests for the explicitly authorized managed-source live smoke command."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import build_settings
from core.cv_import import CvGenerationSelection, compute_cv_reference_hash
from core.generator import (
    GenerationTrace,
    LetterOutput,
    compute_generation_input_hash,
)
from core.source_library import SourceLibrary
from scripts.live_generation_smoke import (
    _smoke_cases,
    _synthetic_source_archive,
)
from scripts.live_managed_source_smoke import (
    _validate_managed_output,
    main,
)


class ManagedSourceSmokeTests(unittest.TestCase):
    """Verify consent gating and non-sensitive managed-output checks."""

    def test_refuses_before_constructing_private_dependencies(self) -> None:
        """No private source may be read before explicit command confirmation."""

        with (
            mock.patch(
                "scripts.live_managed_source_smoke.build_settings"
            ) as build_settings_mock,
            mock.patch(
                "scripts.live_managed_source_smoke.get_client"
            ) as get_client_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "Explicit authorization"):
                main(confirmed=False)

        build_settings_mock.assert_not_called()
        get_client_mock.assert_not_called()

    def test_selects_cv_without_silently_approving_previous_fallback(self) -> None:
        """Managed smoke must let the CV workflow enforce explicit consent."""

        source_library = mock.Mock()
        source_library.is_ready.return_value = True
        cv_workflow = mock.Mock()
        cv_workflow.select_for_generation.return_value = mock.sentinel.selection

        with (
            mock.patch(
                "scripts.live_managed_source_smoke.build_settings",
                return_value=mock.sentinel.settings,
            ),
            mock.patch(
                "scripts.live_managed_source_smoke.SourceLibrary",
                return_value=source_library,
            ),
            mock.patch(
                "scripts.live_managed_source_smoke.get_client",
                return_value=mock.sentinel.client,
            ),
            mock.patch(
                "scripts.live_managed_source_smoke.CvImportWorkflow",
                return_value=cv_workflow,
            ) as workflow_type,
            mock.patch(
                "scripts.live_managed_source_smoke._smoke_cases",
                return_value=(),
            ),
        ):
            result = main(confirmed=True)

        self.assertEqual(result, 0)
        workflow_type.assert_called_once_with(
            mock.sentinel.settings,
            mock.sentinel.client,
        )
        cv_workflow.select_for_generation.assert_called_once_with()

    def test_validates_only_structural_non_sensitive_output_properties(self) -> None:
        """A complete fictional output should pass without examining its claims."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = build_settings(Path(temporary_directory))
            sources = SourceLibrary(settings)
            sources.import_zip(_synthetic_source_archive())
            case = _smoke_cases()[1]
            source_hash = sources.load_bundle(case.language).sha256
            system_prompt = "fictional system"
            user_prompt = "fictional user prompt"
            reference_markdown = "# Profile\n\nAvery Morgan fictional test facts.\n"
            selection = CvGenerationSelection(
                cv_version_id="fictional-managed-cv-v1",
                reference_markdown=reference_markdown,
                cv_reference_hash=compute_cv_reference_hash(reference_markdown),
                used_previous_cv=False,
                warnings=(),
            )
            input_hash = compute_generation_input_hash(
                "generation",
                settings.backend,
                settings.sdk_model,
                source_hash,
                system_prompt,
                user_prompt,
                cv_version_id=selection.cv_version_id,
                cv_reference_hash=selection.cv_reference_hash,
                used_previous_cv=selection.used_previous_cv,
            )
            trace = GenerationTrace(
                operation="generation",
                backend=settings.backend,
                model=settings.sdk_model,
                source_hash=source_hash,
                cv_version_id=selection.cv_version_id,
                cv_reference_hash=selection.cv_reference_hash,
                used_previous_cv=selection.used_previous_cv,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                input_hash=input_hash,
            )
            output = LetterOutput(
                letter=(
                    "Application for Junior C++ Software Engineer\n\n"
                    "Dear Hiring Team,\n\n"
                    + "The role and the work align clearly. " * 35
                    + "\n\nKind regards,\nAvery Morgan"
                ),
                company=case.expected_company,
                role="Junior C++ Software Engineer",
                language=case.language,
                source_hash=source_hash,
                cv_version_id=selection.cv_version_id,
                cv_reference_hash=selection.cv_reference_hash,
                used_previous_cv=selection.used_previous_cv,
                input_hash=input_hash,
                trace=trace,
            )

            _validate_managed_output(output, case, sources, selection)


if __name__ == "__main__":
    unittest.main()
