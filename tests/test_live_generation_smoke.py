"""Tests that the live smoke command cannot load personal project data."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from config import build_settings
from core.source_library import REQUIRED_SOURCE_FILES
from scripts.live_generation_smoke import (
    _MAX_SMOKE_LETTER_WORDS,
    _MIN_SMOKE_LETTER_WORDS,
    _isolated_settings,
    _synthetic_source_archive,
)


class LiveGenerationSmokeIsolationTests(unittest.TestCase):
    """Verify that remote smoke checks use only temporary fictional sources."""

    def setUp(self) -> None:
        """Create one temporary root for path-isolation checks."""

        self._temporary_directory: tempfile.TemporaryDirectory[str] = (
            tempfile.TemporaryDirectory()
        )
        self.root: Path = Path(self._temporary_directory.name).resolve()

    def tearDown(self) -> None:
        """Remove the temporary test directory."""

        self._temporary_directory.cleanup()

    def test_every_private_runtime_path_is_isolated(self) -> None:
        """Smoke settings must never point at the managed personal data paths."""

        settings = _isolated_settings(self.root)
        private_paths = (
            settings.style_examples_dir,
            settings.data_dir,
            settings.source_library_dir,
            settings.uploads_dir,
            settings.cache_dir,
            settings.letters_dir,
            settings.cv_pdf_path,
            settings.cv_reference_path,
            settings.cv_metadata_path,
            settings.applications_path,
            settings.system_prompt_cache_path,
        )
        for path in private_paths:
            self.assertTrue(path.resolve().is_relative_to(self.root))
        self.assertEqual(settings.prompts_dir, build_settings().prompts_dir)

    def test_archive_contains_only_complete_fictional_sources(self) -> None:
        """The smoke archive must be self-contained and identify itself as fake."""

        with zipfile.ZipFile(BytesIO(_synthetic_source_archive())) as archive:
            self.assertEqual(
                set(archive.namelist()),
                set(REQUIRED_SOURCE_FILES),
            )
            bodies = tuple(
                archive.read(filename).decode("utf-8")
                for filename in REQUIRED_SOURCE_FILES
            )

        self.assertIn("fictional smoke-test controller", bodies[0].casefold())
        self.assertIn(
            (
                f"{_MIN_SMOKE_LETTER_WORDS} to "
                f"{_MAX_SMOKE_LETTER_WORDS} words"
            ),
            bodies[0],
        )
        self.assertIn("Avery Morgan", bodies[1])
        self.assertIn("Avery Morgan", bodies[2])
        self.assertTrue(all(body.strip() for body in bodies))


if __name__ == "__main__":
    unittest.main()
