"""Tests for the explicitly authorized managed-source live smoke command."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config import build_settings
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
            input_hash = compute_generation_input_hash(
                "generation",
                settings.backend,
                settings.sdk_model,
                source_hash,
                system_prompt,
                user_prompt,
            )
            trace = GenerationTrace(
                operation="generation",
                backend=settings.backend,
                model=settings.sdk_model,
                source_hash=source_hash,
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
                input_hash=input_hash,
                trace=trace,
            )

            _validate_managed_output(output, case, sources)


if __name__ == "__main__":
    unittest.main()
