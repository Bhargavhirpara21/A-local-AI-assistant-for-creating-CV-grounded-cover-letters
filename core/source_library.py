"""Safe import and live loading for the private cover-letter source library."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from config import Settings

LOGGER = logging.getLogger(__name__)

REQUIRED_SOURCE_FILES: tuple[str, ...] = (
    "cover_letter_instructions.md",
    "bhargav_candidate_profile_en.md",
    "bhargav_candidate_profile_de.md",
    "master_cover_letter_en.md",
    "master_cover_letter_de.md",
)
OPTIONAL_SOURCE_FILES: tuple[str, ...] = ("README.md",)
MANIFEST_FILENAME = "manifest.json"
Language = Literal["de", "en"]


class SourceLibraryError(RuntimeError):
    """Base error for source-library validation or persistence failures."""


class SourceValidationError(SourceLibraryError):
    """Raised when a source ZIP or edited document is unsafe or invalid."""


class SourceNotReadyError(SourceLibraryError):
    """Raised when the managed source library is incomplete or unreadable."""


class SourceConflictError(SourceLibraryError):
    """Raised when an import would replace an existing library without consent."""


class SourceChangedError(SourceLibraryError):
    """Raised when files change repeatedly while a consistent snapshot is read."""


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """One canonical UTF-8 source document and its provenance state."""

    filename: str
    text: str
    sha256: str
    modified_since_import: bool


@dataclass(frozen=True, slots=True)
class SourceBundle:
    """A consistent controller/profile/master snapshot for one language."""

    language: Language
    instructions: SourceDocument
    profile: SourceDocument
    master_letter: SourceDocument
    sha256: str

    @property
    def documents(self) -> tuple[SourceDocument, ...]:
        """Return the bundle documents in deterministic hash/prompt order."""

        return (self.instructions, self.profile, self.master_letter)


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """Non-sensitive result of activating a validated source archive."""

    archive_name: str
    imported_at: str
    filenames: tuple[str, ...]
    replaced_existing: bool


class SourceLibrary:
    """Manage a validated, editable, and freshly loaded private source library."""

    def __init__(
        self,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize the library with explicit settings and an optional clock."""

        self._settings = settings
        self._root = settings.source_library_dir
        self._clock = clock or (lambda: datetime.now(UTC))

    def is_ready(self) -> bool:
        """Return whether all required files and a valid manifest are present."""

        if not self._root.is_dir():
            return False
        if not all((self._root / name).is_file() for name in REQUIRED_SOURCE_FILES):
            return False
        try:
            self._read_manifest()
        except SourceNotReadyError:
            return False
        return True

    def import_zip(
        self,
        zip_bytes: bytes,
        archive_name: str = "source-library.zip",
        *,
        allow_replace: bool = False,
    ) -> ImportSummary:
        """Validate and atomically activate a source-library ZIP archive."""

        files = self._validate_archive(zip_bytes)
        active_exists = self.is_ready()
        if active_exists and not allow_replace:
            raise SourceConflictError(
                "A source library is already active. Confirm replacement before "
                "re-importing; replacement can overwrite your edits."
            )

        imported_at = self._clock().astimezone(UTC).isoformat()
        safe_archive_name = self._sanitize_archive_name(archive_name)
        hashes = {
            filename: self._hash_text(text) for filename, text in files.items()
        }
        manifest: dict[str, object] = {
            "schema_version": 1,
            "imported_at": imported_at,
            "archive_name": safe_archive_name,
            "files": {
                filename: {
                    "sha256": hashes[filename],
                    "size_bytes": len(text.encode("utf-8")),
                }
                for filename, text in sorted(files.items())
            },
        }
        self._activate(files, manifest)
        LOGGER.info(
            "Activated source library with %d files; replacement=%s",
            len(files),
            active_exists,
        )
        return ImportSummary(
            archive_name=safe_archive_name,
            imported_at=imported_at,
            filenames=tuple(sorted(files)),
            replaced_existing=active_exists,
        )

    def load_bundle(self, language: Language) -> SourceBundle:
        """Read a fresh, consistent source snapshot for the selected language."""

        if language not in ("de", "en"):
            raise SourceValidationError(
                f"Unsupported source language {language!r}; expected 'de' or 'en'."
            )
        profile_name = f"bhargav_candidate_profile_{language}.md"
        master_name = f"master_cover_letter_{language}.md"
        filenames = (
            "cover_letter_instructions.md",
            profile_name,
            master_name,
        )
        baseline = self._baseline_hashes()
        texts = self._read_consistent(filenames)
        documents = tuple(
            SourceDocument(
                filename=filename,
                text=texts[filename],
                sha256=self._hash_text(texts[filename]),
                modified_since_import=(
                    baseline.get(filename) != self._hash_text(texts[filename])
                ),
            )
            for filename in filenames
        )
        bundle_hash = self._bundle_hash(documents)
        return SourceBundle(
            language=language,
            instructions=documents[0],
            profile=documents[1],
            master_letter=documents[2],
            sha256=bundle_hash,
        )

    def list_files(self) -> tuple[str, ...]:
        """List active canonical source filenames without exposing contents."""

        if not self.is_ready():
            raise SourceNotReadyError(
                "No complete source library is active. Import the source ZIP first."
            )
        return tuple(
            name
            for name in (*REQUIRED_SOURCE_FILES, *OPTIONAL_SOURCE_FILES)
            if (self._root / name).is_file()
        )

    def read_file(self, filename: str) -> SourceDocument:
        """Read one editable source file from disk using strict UTF-8."""

        self._require_managed_filename(filename)
        baseline = self._baseline_hashes()
        texts = self._read_consistent((filename,))
        text = texts[filename]
        digest = self._hash_text(text)
        return SourceDocument(
            filename=filename,
            text=text,
            sha256=digest,
            modified_since_import=baseline.get(filename) != digest,
        )

    def save_file(self, filename: str, text: str) -> SourceDocument:
        """Validate and atomically save one user-edited managed source file."""

        self._require_managed_filename(filename)
        if not self.is_ready():
            raise SourceNotReadyError(
                "No complete source library is active. Import the source ZIP first."
            )
        canonical = self._canonicalize_text(
            text,
            filename,
            required=filename in REQUIRED_SOURCE_FILES,
        )
        target = self._root / filename
        temporary = self._root / f".{filename}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(canonical, encoding="utf-8", newline="\n")
            os.replace(temporary, target)
        except OSError as error:
            self._remove_file_if_present(temporary)
            LOGGER.exception("Could not atomically save source file %s", filename)
            raise SourceLibraryError(
                f"Could not save {filename}. Check file permissions and try again."
            ) from error
        LOGGER.info("Saved edited source file %s", filename)
        return self.read_file(filename)

    def _validate_archive(self, zip_bytes: bytes) -> dict[str, str]:
        if len(zip_bytes) > self._settings.max_source_zip_bytes:
            raise SourceValidationError(
                "The source ZIP is larger than the allowed 5 MiB limit."
            )
        try:
            archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except (zipfile.BadZipFile, OSError) as error:
            raise SourceValidationError(
                "The uploaded source file is not a valid ZIP archive."
            ) from error

        with archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            if len(entries) > self._settings.max_source_entries:
                raise SourceValidationError(
                    "The source ZIP contains too many files."
                )
            wrapper: str | None = None
            layout: str | None = None
            seen_keys: set[str] = set()
            texts: dict[str, str] = {}
            total_size = 0
            for entry in entries:
                logical_name, entry_wrapper = self._validate_entry_path(entry)
                current_layout = "wrapped" if entry_wrapper is not None else "flat"
                if layout is None:
                    layout = current_layout
                    wrapper = entry_wrapper
                elif layout != current_layout or wrapper != entry_wrapper:
                    raise SourceValidationError(
                        "Use either flat files or one shared top-level folder in "
                        "the source ZIP; mixed layouts are not allowed."
                    )
                normalized_key = unicodedata.normalize(
                    "NFC", logical_name
                ).casefold()
                if normalized_key in seen_keys:
                    raise SourceValidationError(
                        f"Duplicate source filename detected: {logical_name}."
                    )
                seen_keys.add(normalized_key)
                allowed = (*REQUIRED_SOURCE_FILES, *OPTIONAL_SOURCE_FILES)
                if logical_name not in allowed:
                    raise SourceValidationError(
                        f"Unexpected file in source ZIP: {logical_name}."
                    )
                self._validate_entry_type_and_size(entry)
                total_size += entry.file_size
                if total_size > self._settings.max_source_total_bytes:
                    raise SourceValidationError(
                        "The uncompressed source library exceeds the allowed size."
                    )
                try:
                    raw = archive.read(entry)
                except (zipfile.BadZipFile, RuntimeError, OSError) as error:
                    raise SourceValidationError(
                        f"Could not safely read {logical_name} from the source ZIP."
                    ) from error
                try:
                    decoded = raw.decode("utf-8-sig", errors="strict")
                except UnicodeDecodeError as error:
                    raise SourceValidationError(
                        f"{logical_name} is not valid UTF-8."
                    ) from error
                texts[logical_name] = self._canonicalize_text(
                    decoded,
                    logical_name,
                    required=logical_name in REQUIRED_SOURCE_FILES,
                )

            missing = sorted(set(REQUIRED_SOURCE_FILES) - set(texts))
            if missing:
                raise SourceValidationError(
                    "The source ZIP is missing required files: "
                    + ", ".join(missing)
                    + "."
                )
            try:
                bad_member = archive.testzip()
            except (zipfile.BadZipFile, RuntimeError, OSError) as error:
                raise SourceValidationError(
                    "The source ZIP failed its integrity check."
                ) from error
            if bad_member is not None:
                raise SourceValidationError(
                    f"The source ZIP failed its integrity check at {bad_member}."
                )
            return texts

    def _validate_entry_path(
        self,
        entry: zipfile.ZipInfo,
    ) -> tuple[str, str | None]:
        original_name = getattr(entry, "orig_filename", entry.filename)
        if "\x00" in original_name or "\\" in original_name:
            raise SourceValidationError("Unsafe path found in source ZIP.")
        if original_name.startswith(("/", "//")) or re.match(
            r"^[A-Za-z]:", original_name
        ):
            raise SourceValidationError("Absolute paths are not allowed in source ZIP.")
        path = PurePosixPath(original_name)
        parts = path.parts
        if not parts or any(part in ("", ".", "..") for part in parts):
            raise SourceValidationError("Unsafe path found in source ZIP.")
        if len(parts) == 1:
            return parts[0], None
        if len(parts) == 2:
            return parts[1], parts[0]
        raise SourceValidationError(
            "Source files may be flat or inside one shared top-level folder only."
        )

    def _validate_entry_type_and_size(self, entry: zipfile.ZipInfo) -> None:
        if entry.flag_bits & 0x1:
            raise SourceValidationError(
                f"Encrypted source entry is not allowed: {entry.filename}."
            )
        mode = entry.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if file_type not in (0, stat.S_IFREG):
            raise SourceValidationError(
                f"Non-regular source entry is not allowed: {entry.filename}."
            )
        if entry.file_size > self._settings.max_source_file_bytes:
            raise SourceValidationError(
                f"Source file exceeds the size limit: {entry.filename}."
            )
        if entry.file_size > 0 and entry.compress_size == 0:
            raise SourceValidationError(
                f"Invalid compression metadata for source file: {entry.filename}."
            )
        if entry.compress_size > 0:
            ratio = entry.file_size / entry.compress_size
            if ratio > self._settings.max_source_compression_ratio:
                raise SourceValidationError(
                    f"Suspicious compression ratio for source file: {entry.filename}."
                )

    def _canonicalize_text(
        self,
        text: str,
        filename: str,
        *,
        required: bool,
    ) -> str:
        canonical = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        if "\x00" in canonical:
            raise SourceValidationError(f"{filename} contains a NUL character.")
        invalid_controls = [
            character
            for character in canonical
            if ord(character) < 32 and character not in ("\t", "\n")
        ]
        if invalid_controls:
            raise SourceValidationError(
                f"{filename} contains unsupported control characters."
            )
        if required and not canonical.strip():
            raise SourceValidationError(f"{filename} must not be blank.")
        return canonical

    def _activate(
        self,
        files: Mapping[str, str],
        manifest: Mapping[str, object],
    ) -> None:
        parent = self._root.parent
        parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".source-stage-", dir=parent))
        backup = parent / f".source-backup-{uuid.uuid4().hex}"
        moved_old = False
        try:
            for filename, text in files.items():
                (staging / filename).write_text(
                    text,
                    encoding="utf-8",
                    newline="\n",
                )
            (staging / MANIFEST_FILENAME).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            if self._root.exists():
                os.replace(self._root, backup)
                moved_old = True
            os.replace(staging, self._root)
        except OSError as error:
            if moved_old and backup.exists() and not self._root.exists():
                try:
                    os.replace(backup, self._root)
                except OSError:
                    LOGGER.exception("Could not restore source library backup")
            LOGGER.exception("Could not activate validated source library")
            raise SourceLibraryError(
                "Could not activate the source library. The previous library was "
                "kept where recovery was possible."
            ) from error
        finally:
            self._remove_tree_if_present(staging, parent)
        if backup.exists():
            self._remove_tree_if_present(backup, parent)

    def _read_manifest(self) -> dict[str, object]:
        path = self._root / MANIFEST_FILENAME
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise SourceNotReadyError(
                "The source-library manifest is missing or invalid. Re-import "
                "the source ZIP."
            ) from error
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise SourceNotReadyError(
                "The source-library manifest version is unsupported. Re-import "
                "the source ZIP."
            )
        return value

    def _baseline_hashes(self) -> dict[str, str]:
        manifest = self._read_manifest()
        raw_files = manifest.get("files")
        if not isinstance(raw_files, dict):
            raise SourceNotReadyError(
                "The source-library manifest has no valid file records."
            )
        hashes: dict[str, str] = {}
        for filename, raw_record in raw_files.items():
            if not isinstance(filename, str) or not isinstance(raw_record, dict):
                raise SourceNotReadyError(
                    "The source-library manifest contains invalid file records."
                )
            raw_hash = raw_record.get("sha256")
            if not isinstance(raw_hash, str):
                raise SourceNotReadyError(
                    "The source-library manifest contains an invalid file hash."
                )
            hashes[filename] = raw_hash
        return hashes

    def _read_consistent(self, filenames: tuple[str, ...]) -> dict[str, str]:
        if not self.is_ready():
            raise SourceNotReadyError(
                "No complete source library is active. Import the source ZIP first."
            )
        for _attempt in range(2):
            try:
                before = {
                    name: self._file_signature(self._root / name)
                    for name in filenames
                }
                raw_values = {
                    name: (self._root / name).read_bytes() for name in filenames
                }
                after = {
                    name: self._file_signature(self._root / name)
                    for name in filenames
                }
            except OSError as error:
                raise SourceNotReadyError(
                    "A required source file could not be read. Check the managed "
                    "source library and try again."
                ) from error
            if before != after:
                continue
            texts: dict[str, str] = {}
            for name, raw in raw_values.items():
                try:
                    decoded = raw.decode("utf-8-sig", errors="strict")
                except UnicodeDecodeError as error:
                    raise SourceValidationError(
                        f"{name} is not valid UTF-8."
                    ) from error
                texts[name] = self._canonicalize_text(
                    decoded,
                    name,
                    required=name in REQUIRED_SOURCE_FILES,
                )
            return texts
        raise SourceChangedError(
            "Source files changed while they were being read. Retry the operation."
        )

    def _require_managed_filename(self, filename: str) -> None:
        if filename not in (*REQUIRED_SOURCE_FILES, *OPTIONAL_SOURCE_FILES):
            raise SourceValidationError(
                f"{filename!r} is not a managed source-library filename."
            )

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int]:
        metadata = path.stat()
        return metadata.st_size, metadata.st_mtime_ns

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _bundle_hash(documents: tuple[SourceDocument, ...]) -> str:
        digest = hashlib.sha256()
        for document in documents:
            digest.update(document.filename.encode("utf-8"))
            digest.update(b"\0")
            digest.update(document.sha256.encode("ascii"))
            digest.update(b"\n")
        return digest.hexdigest()

    @staticmethod
    def _sanitize_archive_name(archive_name: str) -> str:
        name = archive_name.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
        cleaned = "".join(
            character for character in name if ord(character) >= 32
        ).strip()
        return cleaned or "source-library.zip"

    @staticmethod
    def _remove_file_if_present(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Could not remove temporary source file %s", path.name)

    @staticmethod
    def _remove_tree_if_present(path: Path, expected_parent: Path) -> None:
        try:
            resolved_path = path.resolve()
            resolved_parent = expected_parent.resolve()
            if resolved_path.parent != resolved_parent:
                raise SourceLibraryError(
                    "Refused to remove a temporary path outside the data directory."
                )
            if resolved_path.exists():
                shutil.rmtree(resolved_path)
        except OSError:
            LOGGER.warning("Could not remove temporary source directory %s", path.name)
