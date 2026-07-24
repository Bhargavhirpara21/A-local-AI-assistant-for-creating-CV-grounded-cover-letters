"""Tests for private source-library validation, safety, and live reload."""

from __future__ import annotations

import dataclasses
import io
import json
import stat
import tempfile
import unittest
import zipfile
from collections.abc import Mapping
from pathlib import Path

from config import build_settings
from core.source_library import (
    MANIFEST_FILENAME,
    REQUIRED_SOURCE_FILES,
    SourceConflictError,
    SourceLibrary,
    SourceNotReadyError,
    SourceValidationError,
)


def _source_files() -> dict[str, str]:
    """Return a complete synthetic source set with language sentinels."""

    return {
        "cover_letter_instructions.md": "# Controller\nCONTROLLER_SENTINEL\n",
        "bhargav_candidate_profile_en.md": "# Profile\nEN_PROFILE_SENTINEL\n",
        "bhargav_candidate_profile_de.md": "# Profil\nDE_PROFILE_SENTINEL_ä\n",
        "master_cover_letter_en.md": "# Library\nEN_MASTER_SENTINEL\n",
        "master_cover_letter_de.md": "# Bibliothek\nDE_MASTER_SENTINEL_ö\n",
        "README.md": "# Private source documentation\n",
    }


def _zip_bytes(
    files: Mapping[str, str | bytes],
    *,
    wrapper: str | None = "source",
) -> bytes:
    """Create a synthetic ZIP payload entirely in memory."""

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, value in files.items():
            entry_name = f"{wrapper}/{filename}" if wrapper else filename
            payload = value.encode("utf-8") if isinstance(value, str) else value
            archive.writestr(entry_name, payload)
    return stream.getvalue()


class SourceLibraryTests(unittest.TestCase):
    """Verify safe imports, deterministic routing, and immediate edit visibility."""

    def setUp(self) -> None:
        """Create a private temporary project root for each test."""

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.settings = build_settings(self.root)
        self.library = SourceLibrary(self.settings)

    def tearDown(self) -> None:
        """Release the temporary project root after each test."""

        self._temporary_directory.cleanup()

    def _import_default(self) -> None:
        """Activate the complete synthetic source set."""

        self.library.import_zip(_zip_bytes(_source_files()), "sources.zip")

    def test_accepts_wrapper_and_routes_only_selected_language(self) -> None:
        """The runtime bundle should contain only the selected language pair."""

        summary = self.library.import_zip(
            _zip_bytes(_source_files()),
            r"C:\private\library.zip",
        )

        english = self.library.load_bundle("en")
        german = self.library.load_bundle("de")

        self.assertEqual(summary.archive_name, "library.zip")
        self.assertTrue(self.library.is_ready())
        self.assertIn("EN_PROFILE_SENTINEL", english.profile.text)
        self.assertIn("EN_MASTER_SENTINEL", english.master_letter.text)
        self.assertNotIn("DE_PROFILE_SENTINEL", english.profile.text)
        self.assertIn("DE_PROFILE_SENTINEL", german.profile.text)
        self.assertNotEqual(english.sha256, german.sha256)
        self.assertNotIn("README.md", tuple(doc.filename for doc in english.documents))

    def test_accepts_flat_archive_without_optional_readme(self) -> None:
        """The five required files may be supplied without a wrapper or README."""

        files = _source_files()
        files.pop("README.md")

        self.library.import_zip(_zip_bytes(files, wrapper=None))

        self.assertEqual(set(self.library.list_files()), set(REQUIRED_SOURCE_FILES))

    def test_reports_every_missing_required_file(self) -> None:
        """A missing required document should fail with its exact filename."""

        for missing in REQUIRED_SOURCE_FILES:
            with self.subTest(missing=missing):
                files = _source_files()
                files.pop(missing)
                with self.assertRaisesRegex(SourceValidationError, missing):
                    self.library.import_zip(_zip_bytes(files))

    def test_rejects_unsafe_and_unknown_paths(self) -> None:
        """Traversal, absolute, nested, mixed, and unknown paths must be rejected."""

        base = _source_files()
        cases = {
            "traversal": ("../evil.md", "x"),
            "absolute": ("/evil.md", "x"),
            "drive": ("C:/evil.md", "x"),
            "backslash": (r"..\evil.md", "x"),
            "nested": ("outer/inner/evil.md", "x"),
            "unknown": ("source/notes.exe", "x"),
        }
        for name, (entry_name, value) in cases.items():
            with self.subTest(case=name):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
                    for filename, text in base.items():
                        archive.writestr(f"source/{filename}", text)
                    archive.writestr(entry_name, value)
                with self.assertRaises(SourceValidationError):
                    self.library.import_zip(stream.getvalue())
                self.assertFalse(self.library.is_ready())

    def test_rejects_case_fold_duplicate_and_symlink(self) -> None:
        """Logical duplicate names and non-regular entries must be rejected."""

        files = _source_files()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename, text in files.items():
                archive.writestr(f"source/{filename}", text)
            archive.writestr("source/README.MD", "duplicate")
        with self.assertRaisesRegex(SourceValidationError, "Duplicate"):
            self.library.import_zip(stream.getvalue())

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename, text in files.items():
                info = zipfile.ZipInfo(f"source/{filename}")
                if filename == "README.md":
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, text)
        with self.assertRaisesRegex(SourceValidationError, "Non-regular"):
            self.library.import_zip(stream.getvalue())

    def test_rejects_invalid_utf8_blank_and_controls(self) -> None:
        """Invalid encodings and unsafe/blank required text should fail closed."""

        invalid_cases: tuple[tuple[str, str | bytes], ...] = (
            ("bhargav_candidate_profile_en.md", b"\xff\xfe"),
            ("cover_letter_instructions.md", " \r\n\t"),
            ("master_cover_letter_de.md", "text\x00hidden"),
            ("master_cover_letter_en.md", "text\x07bell"),
        )
        for filename, invalid_value in invalid_cases:
            with self.subTest(filename=filename):
                files: dict[str, str | bytes] = dict(_source_files())
                files[filename] = invalid_value
                with self.assertRaisesRegex(SourceValidationError, filename):
                    self.library.import_zip(_zip_bytes(files))
                self.assertFalse(self.library.is_ready())

    def test_enforces_configured_archive_size_limits(self) -> None:
        """Configurable file and entry limits should be enforced before activation."""

        limited_settings = dataclasses.replace(
            self.settings,
            max_source_file_bytes=10,
        )
        limited_library = SourceLibrary(limited_settings)

        with self.assertRaisesRegex(SourceValidationError, "size limit"):
            limited_library.import_zip(_zip_bytes(_source_files()))

    def test_failed_or_unconfirmed_reimport_preserves_active_library(self) -> None:
        """Replacement must be explicit and invalid input must not alter active files."""

        self._import_default()
        original = self.library.read_file("cover_letter_instructions.md")
        changed = _source_files()
        changed["cover_letter_instructions.md"] = "# Changed\n"

        with self.assertRaises(SourceConflictError):
            self.library.import_zip(_zip_bytes(changed), allow_replace=False)
        self.assertEqual(
            self.library.read_file("cover_letter_instructions.md").sha256,
            original.sha256,
        )

        changed.pop("master_cover_letter_en.md")
        with self.assertRaises(SourceValidationError):
            self.library.import_zip(_zip_bytes(changed), allow_replace=True)
        self.assertEqual(
            self.library.read_file("cover_letter_instructions.md").sha256,
            original.sha256,
        )

    def test_confirmed_reimport_replaces_all_files_and_manifest(self) -> None:
        """A confirmed valid replacement should activate one complete new snapshot."""

        self._import_default()
        changed = _source_files()
        changed["cover_letter_instructions.md"] = "# Changed controller\n"

        summary = self.library.import_zip(
            _zip_bytes(changed),
            allow_replace=True,
        )

        self.assertTrue(summary.replaced_existing)
        self.assertEqual(
            self.library.read_file("cover_letter_instructions.md").text,
            "# Changed controller\n",
        )
        manifest = json.loads(
            (self.settings.source_library_dir / MANIFEST_FILENAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(manifest["schema_version"], 1)

    def test_live_edit_changes_next_bundle_without_restart(self) -> None:
        """A selected-file edit should appear in the next load and change its hash."""

        self._import_default()
        before = self.library.load_bundle("en")

        edited = self.library.save_file(
            "bhargav_candidate_profile_en.md",
            before.profile.text + "LIVE_EDIT_SENTINEL\n",
        )
        after = self.library.load_bundle("en")

        self.assertTrue(edited.modified_since_import)
        self.assertIn("LIVE_EDIT_SENTINEL", after.profile.text)
        self.assertNotEqual(before.sha256, after.sha256)

    def test_opposite_language_edit_does_not_change_bundle_hash(self) -> None:
        """A German-only edit should not alter the current English snapshot."""

        self._import_default()
        before = self.library.load_bundle("en")
        german = self.library.read_file("bhargav_candidate_profile_de.md")

        self.library.save_file(
            german.filename,
            german.text + "NUR_DEUTSCH\n",
        )
        after = self.library.load_bundle("en")

        self.assertEqual(before.sha256, after.sha256)

    def test_bom_and_windows_newlines_are_canonicalized(self) -> None:
        """UTF-8 BOM and CRLF should save as stable UTF-8 with LF newlines."""

        files = _source_files()
        files["cover_letter_instructions.md"] = "\ufeff# Rules\r\nLine\r\n"

        self.library.import_zip(_zip_bytes(files))

        raw = (
            self.settings.source_library_dir / "cover_letter_instructions.md"
        ).read_bytes()
        self.assertEqual(raw, b"# Rules\nLine\n")

    def test_invalid_edit_preserves_previous_file(self) -> None:
        """An invalid in-app edit must not damage the last valid source."""

        self._import_default()
        original = self.library.read_file("cover_letter_instructions.md")

        with self.assertRaises(SourceValidationError):
            self.library.save_file("cover_letter_instructions.md", "\x00")

        self.assertEqual(
            self.library.read_file("cover_letter_instructions.md").sha256,
            original.sha256,
        )

    def test_missing_active_file_never_falls_back(self) -> None:
        """Deleting a required selected file should make loading fail closed."""

        self._import_default()
        (
            self.settings.source_library_dir
            / "bhargav_candidate_profile_en.md"
        ).unlink()

        with self.assertRaises(SourceNotReadyError):
            self.library.load_bundle("en")

    def test_invalid_language_is_rejected(self) -> None:
        """Only explicit German and English language identifiers are accepted."""

        self._import_default()

        with self.assertRaises(SourceValidationError):
            self.library.load_bundle("fr")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
