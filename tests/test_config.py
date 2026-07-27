"""Tests for immutable settings and runtime directory construction."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path

from config import build_settings, ensure_dirs


class SettingsTests(unittest.TestCase):
    """Verify settings stay explicit, immutable, and rooted correctly."""

    def test_build_settings_resolves_all_private_paths_under_root(self) -> None:
        """Private paths should be deterministic children of the project root."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            settings = build_settings(root)

            self.assertEqual(settings.project_root, root)
            self.assertEqual(settings.data_dir, root / "data")
            self.assertEqual(
                settings.source_library_dir,
                root / "data" / "source_library",
            )
            self.assertEqual(
                settings.applications_path,
                root / "data" / "applications.xlsx",
            )
            self.assertEqual(settings.cv_dir, root / "data" / "cv")
            self.assertEqual(
                settings.cv_versions_dir,
                root / "data" / "cv" / "versions",
            )
            self.assertEqual(
                settings.cv_staging_dir,
                root / "data" / "cv" / "staging",
            )
            self.assertEqual(
                settings.cv_active_path,
                root / "data" / "cv" / "active.json",
            )
            self.assertEqual(
                settings.cv_pending_path,
                root / "data" / "cv" / "pending.json",
            )
            self.assertEqual(
                settings.cv_pending_recovery_path,
                root / "data" / "cv" / "pending.recovery.json",
            )
            self.assertFalse(hasattr(settings, "cv_pdf_path"))
            self.assertFalse(hasattr(settings, "cv_reference_path"))
            self.assertFalse(hasattr(settings, "cv_metadata_path"))
            self.assertEqual(settings.max_cv_pdf_bytes, 25 * 1024 * 1024)
            self.assertEqual(
                settings.cv_import_max_buffer_bytes,
                64 * 1024 * 1024,
            )
            self.assertEqual(settings.letters_dir, root / "letters")

    def test_settings_are_frozen(self) -> None:
        """Callers must not mutate a shared configuration object."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = build_settings(Path(temporary_directory))

            with self.assertRaises(dataclasses.FrozenInstanceError):
                settings.backend = "unexpected"  # type: ignore[misc]

    def test_ensure_dirs_creates_every_runtime_directory(self) -> None:
        """Directory initialization should create all runtime parents."""

        with tempfile.TemporaryDirectory() as temporary_directory:
            settings = build_settings(Path(temporary_directory))

            ensure_dirs(settings)

            expected_directories = (
                settings.prompts_dir,
                settings.style_examples_dir,
                settings.data_dir,
                settings.source_library_dir,
                settings.uploads_dir,
                settings.cache_dir,
                settings.cv_dir,
                settings.cv_versions_dir,
                settings.cv_staging_dir,
                settings.letters_dir,
            )
            self.assertTrue(all(path.is_dir() for path in expected_directories))


if __name__ == "__main__":
    unittest.main()
