"""Versioned, review-gated, and consent-aware private CV persistence."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from config import Settings
from llm.base import LLMClient

LOGGER = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_MAX_REFERENCE_BYTES = 5 * 1024 * 1024
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_WINDOWS_UNSAFE_NAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_PATTERN = re.compile(r"\s+")
_WINDOWS_RESERVED_STEMS: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)

CvPendingStatus = Literal["extracting", "review", "failed"]


class CvImportError(RuntimeError):
    """Base error for CV validation, extraction, persistence, or selection."""


class CvValidationError(CvImportError):
    """Raised when PDF, reference, warning, or selection data is invalid."""


class CvPendingExistsError(CvImportError):
    """Raised when a new upload would silently replace a pending attempt."""


class CvPendingNotFoundError(CvImportError):
    """Raised when a pending-only operation has no pending import."""


class CvPendingStateError(CvImportError):
    """Raised when an operation is invalid for the current pending state."""


class CvCorruptPendingError(CvImportError):
    """Raised when persisted pending state or its staged files fail validation."""


class CvExtractionError(CvImportError):
    """Raised after a failed extraction has been persisted for retry."""


class CvReviewRequiredError(CvImportError):
    """Raised when confirmation is attempted before a review draft exists."""


class CvPublicationError(CvImportError):
    """Raised when local CV state cannot be published atomically."""


class CvCorruptVersionError(CvImportError):
    """Raised when an active pointer or confirmed bundle fails verification."""


class CvConsentRequiredError(CvImportError):
    """Raised when pending new-CV work blocks silent use of the old CV."""


class CvFallbackUnavailableError(CvImportError):
    """Raised when previous-CV fallback is requested without a valid active CV."""


class CvNotReadyError(CvImportError):
    """Raised when generation is requested without a confirmed CV."""


@dataclass(frozen=True, slots=True)
class CvPendingImport:
    """Persistent state for one staged, extracting, review, or failed import."""

    attempt_id: str
    status: CvPendingStatus
    original_name: str
    started_at: str
    updated_at: str
    pdf_sha256: str | None
    pdf_size_bytes: int
    has_staged_pdf: bool
    previous_cv_version_id: str | None
    reference_markdown: str | None = field(default=None, repr=False)
    reference_sha256: str | None = None
    warnings: tuple[str, ...] = ()
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CvVersionBundle:
    """One verified immutable PDF, reviewed reference, and metadata bundle."""

    cv_version_id: str
    confirmed_at: str
    original_name: str
    pdf_sha256: str
    pdf_size_bytes: int
    reference_markdown: str = field(repr=False)
    cv_reference_hash: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CvGenerationSelection:
    """A fully verified CV reference and provenance approved for one generation."""

    cv_version_id: str
    reference_markdown: str = field(repr=False)
    cv_reference_hash: str
    used_previous_cv: bool
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Fail closed when a manually constructed selection is inconsistent."""

        _validate_identifier(self.cv_version_id, "CV version")
        canonical = _canonicalize_reference(self.reference_markdown)
        if canonical != self.reference_markdown:
            raise CvValidationError(
                "The selected CV reference is not in canonical UTF-8/LF form."
            )
        _validate_sha256(self.cv_reference_hash, "CV reference")
        if compute_cv_reference_hash(self.reference_markdown) != self.cv_reference_hash:
            raise CvValidationError(
                "The selected CV reference does not match its recorded hash."
            )
        if type(self.used_previous_cv) is not bool:
            raise CvValidationError(
                "The previous-CV provenance flag must be a boolean."
            )
        normalized_warnings = _validate_warnings(self.warnings)
        if normalized_warnings != self.warnings:
            raise CvValidationError(
                "CV selection warnings must already be normalized."
            )


def compute_cv_reference_hash(reference_markdown: str) -> str:
    """Hash the exact UTF-8 bytes of a canonical reviewed CV reference."""

    if not isinstance(reference_markdown, str):
        raise CvValidationError("The CV reference must be text.")
    try:
        encoded = reference_markdown.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CvValidationError("The CV reference is not valid UTF-8 text.") from error
    return hashlib.sha256(encoded).hexdigest()


class CvImportWorkflow:
    """Manage safe CV staging, extraction, review, activation, and selection."""

    _settings: Settings
    _client: LLMClient
    _clock: Callable[[], datetime]
    _id_factory: Callable[[], str]

    def __init__(
        self,
        settings: Settings,
        client: LLMClient,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        """Initialize the workflow with explicit storage and model dependencies."""

        self._settings = settings
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)

    def start_import(
        self,
        file_bytes: bytes,
        original_name: str = "cv.pdf",
    ) -> CvPendingImport:
        """Validate and durably stage a PDF before any model call occurs."""

        if self.load_pending() is not None:
            raise CvPendingExistsError(
                "A CV import is already pending. Retry, confirm, or discard it "
                "before uploading another CV."
            )
        self._ensure_directories()
        try:
            previous = self.load_active()
        except CvCorruptVersionError:
            LOGGER.warning(
                "Existing active CV is invalid and cannot be offered as fallback"
            )
            previous = None
        attempt_id = self._new_identifier("CV import attempt")
        timestamp = self._timestamp()
        previous_version_id = (
            previous.cv_version_id if previous is not None else None
        )
        try:
            self._write_pending_recovery(
                attempt_id,
                previous_version_id,
            )
        except OSError as persistence_error:
            LOGGER.warning("CV safety recovery marker persistence failed")
            raise CvPublicationError(
                "The CV update could not be recorded safely. Do not generate a "
                "letter in this session; restore data-directory permissions, "
                "then retry or restart the app."
            ) from persistence_error
        try:
            safe_name = _safe_pdf_basename(original_name)
            self._validate_pdf_bytes(file_bytes)
        except CvValidationError as validation_error:
            rejected = CvPendingImport(
                attempt_id=attempt_id,
                status="failed",
                original_name="cv.pdf",
                started_at=timestamp,
                updated_at=timestamp,
                pdf_sha256=None,
                pdf_size_bytes=0,
                has_staged_pdf=False,
                previous_cv_version_id=previous_version_id,
                error_message=str(validation_error),
            )
            try:
                self._write_pending(rejected)
            except OSError as persistence_error:
                LOGGER.warning("Rejected CV safety marker persistence failed")
                raise CvPublicationError(
                    "The CV was rejected, but its safety marker could not be "
                    "saved. Do not generate a letter until data-directory "
                    "permissions are restored and the upload is retried."
                ) from persistence_error
            LOGGER.info("Rejected CV upload recorded as a blocking attempt")
            raise
        staging_blocker = CvPendingImport(
            attempt_id=attempt_id,
            status="failed",
            original_name=safe_name,
            started_at=timestamp,
            updated_at=timestamp,
            pdf_sha256=None,
            pdf_size_bytes=0,
            has_staged_pdf=False,
            previous_cv_version_id=previous_version_id,
            error_message=(
                "The CV upload did not finish staging. Discard this attempt and "
                "choose the PDF again."
            ),
        )
        try:
            self._write_pending(staging_blocker)
        except OSError as persistence_error:
            LOGGER.warning("CV staging safety marker persistence failed")
            raise CvPublicationError(
                "The CV update could not be staged safely. Do not generate a "
                "letter until the pending CV problem is discarded or repaired."
            ) from persistence_error
        stage_path = self._settings.cv_staging_dir / attempt_id
        temporary_stage = (
            self._settings.cv_staging_dir
            / f".pending-{uuid.uuid4().hex}.tmp"
        )
        if stage_path.exists():
            raise CvPublicationError(
                "Could not stage the CV because a private import identifier "
                "already exists. Retry the upload."
            )
        pdf_hash = hashlib.sha256(file_bytes).hexdigest()
        pending = CvPendingImport(
            attempt_id=attempt_id,
            status="extracting",
            original_name=safe_name,
            started_at=timestamp,
            updated_at=timestamp,
            pdf_sha256=pdf_hash,
            pdf_size_bytes=len(file_bytes),
            has_staged_pdf=True,
            previous_cv_version_id=previous_version_id,
        )
        try:
            temporary_stage.mkdir(parents=False, exist_ok=False)
            _write_new_bytes(temporary_stage / "cv.pdf", file_bytes)
            os.replace(temporary_stage, stage_path)
            self._write_pending(pending)
        except OSError as error:
            _remove_tree_quietly(
                temporary_stage,
                self._settings.cv_staging_dir,
            )
            _remove_tree_quietly(stage_path, self._settings.cv_staging_dir)
            LOGGER.warning("CV import staging failed")
            raise CvPublicationError(
                "Could not save the uploaded CV in private staging. Check data "
                "directory permissions and try again."
            ) from error
        LOGGER.info("CV import attempt staged")
        return pending

    def extract_pending(self) -> CvPendingImport:
        """Extract the staged PDF into reviewable Markdown using the model client."""

        pending = self._require_pending()
        if pending.status != "extracting":
            if pending.status == "failed":
                raise CvPendingStateError(
                    "The CV extraction previously failed. Use retry to process "
                    "the staged PDF again."
                )
            raise CvPendingStateError(
                "This CV already has a review draft. Review and confirm it, or "
                "discard the attempt."
            )
        pdf_path = self._verified_pending_pdf_path(pending)
        try:
            result = self._client.import_cv(pdf_path)
        except (OSError, RuntimeError) as error:
            self._persist_failed_extraction(
                pending,
                "Claude could not read the staged CV. Retry without uploading it "
                "again.",
            )
            LOGGER.warning("CV extraction failed")
            raise CvExtractionError(
                "Claude could not read the staged CV. Retry the pending import."
            ) from error
        self._verified_pending_pdf_path(pending)
        if result.is_error:
            message = (
                result.error_message
                or "Claude could not extract the CV. Retry the pending import."
            )
            self._persist_failed_extraction(pending, message)
            LOGGER.warning("CV extraction failed")
            raise CvExtractionError(message)
        try:
            reference = _canonicalize_reference(result.text)
        except CvValidationError as error:
            message = (
                "Claude did not return a reviewable '# Profile' Markdown "
                "reference. Retry the pending import."
            )
            self._persist_failed_extraction(pending, message)
            LOGGER.warning("CV extraction returned invalid review text")
            raise CvExtractionError(message) from error
        reference_hash = compute_cv_reference_hash(reference)
        reference_path = self._pending_directory(pending.attempt_id) / "reference.md"
        review = dataclasses.replace(
            pending,
            status="review",
            updated_at=self._timestamp(),
            reference_markdown=reference,
            reference_sha256=reference_hash,
            error_message=None,
        )
        try:
            _atomic_write_bytes(reference_path, reference.encode("utf-8"))
            self._write_pending(review)
        except OSError as error:
            self._persist_failed_extraction(
                pending,
                "The extracted CV could not be saved for review. Check data "
                "directory permissions and retry.",
            )
            LOGGER.warning("CV review draft persistence failed")
            raise CvPublicationError(
                "The extracted CV could not be saved for review. Check data "
                "directory permissions and retry."
            ) from error
        LOGGER.info("CV extraction is ready for review")
        return review

    def retry_pending(self) -> CvPendingImport:
        """Retry extraction from the existing exact staged PDF bytes."""

        pending = self._require_pending()
        if pending.status == "review":
            raise CvPendingStateError(
                "The CV is already ready for review and does not need extraction."
            )
        if not pending.has_staged_pdf:
            raise CvPendingStateError(
                "The rejected upload has no staged PDF to retry. Discard this "
                "attempt, choose another PDF, and upload it again."
            )
        self._verified_pending_pdf_path(pending)
        extracting = dataclasses.replace(
            pending,
            status="extracting",
            updated_at=self._timestamp(),
            reference_markdown=None,
            reference_sha256=None,
            error_message=None,
        )
        reference_path = self._pending_directory(pending.attempt_id) / "reference.md"
        try:
            reference_path.unlink(missing_ok=True)
            self._write_pending(extracting)
        except OSError as error:
            LOGGER.warning("CV retry state persistence failed")
            raise CvPublicationError(
                "Could not prepare the pending CV for retry. Check data directory "
                "permissions and try again."
            ) from error
        return self.extract_pending()

    def load_pending(self) -> CvPendingImport | None:
        """Load and verify the current persistent pending import, if one exists."""

        pending_exists = self._settings.cv_pending_path.exists()
        recovery_exists = self._settings.cv_pending_recovery_path.exists()
        if not pending_exists and not recovery_exists:
            return None
        if not pending_exists or not recovery_exists:
            raise CvCorruptPendingError(
                "The pending CV safety records are incomplete. Discard or repair "
                "the pending import before continuing."
            )
        recovery_attempt_id, recovery_previous_version_id = (
            self._load_pending_recovery()
        )
        record = self._read_json(
            self._settings.cv_pending_path,
            CvCorruptPendingError,
            "The pending CV record is unreadable. Discard or repair the pending "
            "import before continuing.",
        )
        try:
            if record.get("schema_version") != _SCHEMA_VERSION:
                raise CvCorruptPendingError(
                    "The pending CV record uses an unsupported schema."
                )
            attempt_id = _mapping_string(record, "attempt_id")
            _validate_identifier(attempt_id, "CV import attempt")
            raw_status = _mapping_string(record, "status")
            if raw_status not in ("extracting", "review", "failed"):
                raise CvCorruptPendingError(
                    "The pending CV record has an invalid workflow state."
                )
            status: CvPendingStatus = raw_status
            original_name = _mapping_string(record, "original_name")
            if _safe_pdf_basename(original_name) != original_name:
                raise CvCorruptPendingError(
                    "The pending CV record has an invalid original filename."
                )
            started_at = _validated_timestamp(
                _mapping_string(record, "started_at")
            )
            updated_at = _validated_timestamp(
                _mapping_string(record, "updated_at")
            )
            pdf_sha256 = _optional_mapping_string(record, "pdf_sha256")
            pdf_size_bytes = _mapping_integer(record, "pdf_size_bytes")
            has_staged_pdf = _mapping_boolean(record, "has_staged_pdf")
            if has_staged_pdf:
                if pdf_sha256 is None:
                    raise CvCorruptPendingError(
                        "The pending CV record is missing its PDF hash."
                    )
                _validate_sha256(pdf_sha256, "pending CV PDF")
                if pdf_size_bytes <= 0:
                    raise CvCorruptPendingError(
                        "The pending CV record has an invalid PDF size."
                    )
            elif (
                raw_status != "failed"
                or pdf_sha256 is not None
                or pdf_size_bytes != 0
            ):
                raise CvCorruptPendingError(
                    "The pending CV record has inconsistent rejected-upload data."
                )
            previous_cv_version_id = _optional_mapping_string(
                record,
                "previous_cv_version_id",
            )
            if previous_cv_version_id is not None:
                _validate_identifier(
                    previous_cv_version_id,
                    "previous CV version",
                )
            if (
                attempt_id != recovery_attempt_id
                or previous_cv_version_id != recovery_previous_version_id
            ):
                raise CvCorruptPendingError(
                    "The pending CV record does not match its recovery marker."
                )
            warnings = _warnings_from_json(record.get("warnings"))
            error_message = _optional_mapping_string(record, "error_message")
            reference_hash = _optional_mapping_string(
                record,
                "reference_sha256",
            )
            pending = CvPendingImport(
                attempt_id=attempt_id,
                status=status,
                original_name=original_name,
                started_at=started_at,
                updated_at=updated_at,
                pdf_sha256=pdf_sha256,
                pdf_size_bytes=pdf_size_bytes,
                has_staged_pdf=has_staged_pdf,
                previous_cv_version_id=previous_cv_version_id,
                reference_sha256=reference_hash,
                warnings=warnings,
                error_message=error_message,
            )
            if has_staged_pdf:
                self._verified_pending_pdf_path(pending)
            reference: str | None = None
            if status == "review":
                if reference_hash is None:
                    raise CvCorruptPendingError(
                        "The pending review is missing its reference hash."
                    )
                reference = self._read_verified_reference(
                    self._pending_directory(attempt_id) / "reference.md",
                    reference_hash,
                    CvCorruptPendingError,
                )
            elif reference_hash is not None:
                raise CvCorruptPendingError(
                    "The pending CV record has reference data in an invalid state."
                )
            if status == "failed" and not error_message:
                raise CvCorruptPendingError(
                    "The failed CV import has no actionable failure message."
                )
            if status != "failed" and error_message is not None:
                raise CvCorruptPendingError(
                    "The pending CV record has a failure message in an invalid state."
                )
            return dataclasses.replace(
                pending,
                reference_markdown=reference,
            )
        except CvValidationError as error:
            raise CvCorruptPendingError(
                "The pending CV record or staged files failed validation."
            ) from error

    def confirm_pending(
        self,
        reviewed_reference: str,
        *,
        warnings: tuple[str, ...] = (),
    ) -> CvVersionBundle:
        """Publish a reviewed reference bundle, then atomically select it."""

        pending = self._require_pending()
        if pending.status != "review" or pending.reference_markdown is None:
            raise CvReviewRequiredError(
                "Finish CV extraction and review the draft before confirming it."
            )
        reference = _canonicalize_reference(reviewed_reference)
        supplied_warnings = _validate_warnings(warnings)
        normalized_warnings = _validate_warnings(
            (*pending.warnings, *supplied_warnings)
        )
        pdf_path = self._verified_pending_pdf_path(pending)
        try:
            pdf_bytes = pdf_path.read_bytes()
        except OSError as error:
            raise CvCorruptPendingError(
                "The staged CV PDF could not be read for confirmation."
            ) from error
        version_id = self._new_identifier("CV version")
        version_path = self._settings.cv_versions_dir / version_id
        temporary_version = (
            self._settings.cv_versions_dir
            / f".version-{uuid.uuid4().hex}.tmp"
        )
        if version_path.exists():
            raise CvPublicationError(
                "Could not publish the CV because a private version identifier "
                "already exists. Retry confirmation."
            )
        confirmed_at = self._timestamp()
        reference_hash = compute_cv_reference_hash(reference)
        metadata: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "version_id": version_id,
            "confirmed_at": confirmed_at,
            "original_name": pending.original_name,
            "pdf_sha256": pending.pdf_sha256,
            "pdf_size_bytes": pending.pdf_size_bytes,
            "cv_reference_hash": reference_hash,
            "warnings": list(normalized_warnings),
        }
        try:
            temporary_version.mkdir(parents=False, exist_ok=False)
            _write_new_bytes(temporary_version / "cv.pdf", pdf_bytes)
            _write_new_bytes(
                temporary_version / "reference.md",
                reference.encode("utf-8"),
            )
            _write_new_bytes(
                temporary_version / "metadata.json",
                _json_bytes(metadata),
            )
            os.replace(temporary_version, version_path)
        except OSError as error:
            _remove_tree_quietly(
                temporary_version,
                self._settings.cv_versions_dir,
            )
            LOGGER.warning("CV version bundle publication failed")
            raise CvPublicationError(
                "Could not publish the reviewed CV bundle. The previous active "
                "CV and pending review were kept."
            ) from error

        try:
            published = self._load_version(version_id)
        except CvCorruptVersionError:
            LOGGER.warning("Published CV bundle failed verification")
            raise
        pointer: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "version_id": version_id,
            "activated_at": confirmed_at,
            "pdf_sha256": published.pdf_sha256,
            "cv_reference_hash": published.cv_reference_hash,
        }
        try:
            _atomic_write_bytes(
                self._settings.cv_active_path,
                _json_bytes(pointer),
            )
        except OSError as error:
            LOGGER.warning("CV active pointer publication failed")
            raise CvPublicationError(
                "Could not activate the reviewed CV. The previous active CV and "
                "pending review were kept."
            ) from error

        try:
            self._settings.cv_pending_path.unlink(missing_ok=False)
        except OSError as error:
            LOGGER.warning("Confirmed CV pending-state cleanup failed")
            raise CvPublicationError(
                "The reviewed CV was activated, but its pending marker could not "
                "be cleared. Check data directory permissions before continuing."
            ) from error
        try:
            self._remove_pending_stage(pending.attempt_id)
            self._settings.cv_pending_recovery_path.unlink(missing_ok=False)
        except OSError as error:
            LOGGER.warning("Confirmed CV recovery-state cleanup failed")
            raise CvPublicationError(
                "The reviewed CV was activated, but private import cleanup did "
                "not finish. Resolve or discard the pending CV problem before "
                "generating a letter."
            ) from error
        LOGGER.info("Reviewed CV version activated")
        return published

    def discard_pending(self) -> bool:
        """Explicitly remove the current pending attempt without changing active CV."""

        if (
            not self._settings.cv_pending_path.exists()
            and not self._settings.cv_pending_recovery_path.exists()
        ):
            return False
        try:
            pending = self.load_pending()
            attempt_id = pending.attempt_id if pending is not None else None
        except CvCorruptPendingError:
            attempt_id, _ = self._recover_pending_identity()
            LOGGER.warning("Discarding a corrupt pending CV marker")
        try:
            self._settings.cv_pending_path.unlink(missing_ok=True)
        except OSError as error:
            LOGGER.warning("Pending CV discard failed")
            raise CvPublicationError(
                "Could not discard the pending CV. Check data directory "
                "permissions and try again."
            ) from error
        try:
            if attempt_id is not None:
                self._remove_pending_stage(attempt_id)
            self._settings.cv_pending_recovery_path.unlink(missing_ok=True)
        except OSError as error:
            LOGGER.warning("Pending CV recovery cleanup failed")
            raise CvPublicationError(
                "The pending CV marker was cleared, but its exact private staging "
                "cleanup did not finish. Retry discard before generating."
            ) from error
        LOGGER.info("Pending CV import discarded")
        return True

    def load_active(self) -> CvVersionBundle | None:
        """Load the active pointer and fully verify its immutable bundle."""

        if not self._settings.cv_active_path.exists():
            return None
        pointer = self._read_json(
            self._settings.cv_active_path,
            CvCorruptVersionError,
            "The active CV pointer is unreadable. Reconfirm a valid CV version.",
        )
        try:
            if pointer.get("schema_version") != _SCHEMA_VERSION:
                raise CvCorruptVersionError(
                    "The active CV pointer uses an unsupported schema."
                )
            version_id = _mapping_string(pointer, "version_id")
            _validate_identifier(version_id, "active CV version")
            _validated_timestamp(_mapping_string(pointer, "activated_at"))
            pointer_pdf_hash = _mapping_string(pointer, "pdf_sha256")
            pointer_reference_hash = _mapping_string(
                pointer,
                "cv_reference_hash",
            )
            _validate_sha256(pointer_pdf_hash, "active CV PDF")
            _validate_sha256(pointer_reference_hash, "active CV reference")
            version = self._load_version(version_id)
            if (
                version.pdf_sha256 != pointer_pdf_hash
                or version.cv_reference_hash != pointer_reference_hash
            ):
                raise CvCorruptVersionError(
                    "The active CV pointer does not match its version bundle."
                )
            return version
        except CvValidationError as error:
            raise CvCorruptVersionError(
                "The active CV pointer or version bundle failed validation."
            ) from error

    def select_for_generation(
        self,
        *,
        allow_previous: bool = False,
    ) -> CvGenerationSelection:
        """Return a verified selection, requiring consent while a new CV is pending."""

        if type(allow_previous) is not bool:
            raise CvValidationError(
                "Previous-CV consent must be an explicit boolean."
            )
        try:
            pending = self.load_pending()
        except CvCorruptPendingError as error:
            if not allow_previous:
                raise CvConsentRequiredError(
                    "A newer CV upload has a storage problem. Repair or discard "
                    "it. To continue this application with the old CV, explicitly "
                    "choose 'Use previous CV for this application'."
                ) from error
            _, previous_version_id = self._recover_pending_identity()
            return self._select_previous_version(
                previous_version_id,
                pending_status="corrupt",
            )
        if pending is not None:
            if not allow_previous:
                raise CvConsentRequiredError(
                    "A newer CV import is pending or failed. Retry, review, confirm, "
                    "or discard it. To continue this application with the old CV, "
                    "explicitly choose 'Use previous CV for this application'."
                )
            return self._select_previous_version(
                pending.previous_cv_version_id,
                pending_status=pending.status,
            )
        active = self.load_active()
        if active is None:
            raise CvNotReadyError(
                "No confirmed CV is available. Import, review, and confirm a CV "
                "before generating a letter."
            )
        return CvGenerationSelection(
            cv_version_id=active.cv_version_id,
            reference_markdown=active.reference_markdown,
            cv_reference_hash=active.cv_reference_hash,
            used_previous_cv=False,
            warnings=active.warnings,
        )

    def _persist_failed_extraction(
        self,
        pending: CvPendingImport,
        message: str,
    ) -> None:
        cleaned_message = message.strip() or (
            "Claude could not extract the CV. Retry the pending import."
        )
        failed = dataclasses.replace(
            pending,
            status="failed",
            updated_at=self._timestamp(),
            reference_markdown=None,
            reference_sha256=None,
            error_message=cleaned_message,
        )
        reference_path = self._pending_directory(pending.attempt_id) / "reference.md"
        try:
            reference_path.unlink(missing_ok=True)
            self._write_pending(failed)
        except OSError as error:
            LOGGER.warning("CV failure-state persistence failed")
            raise CvPublicationError(
                "CV extraction failed and its retry state could not be saved. "
                "Check data directory permissions before continuing."
            ) from error

    def _write_pending(self, pending: CvPendingImport) -> None:
        record: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "attempt_id": pending.attempt_id,
            "status": pending.status,
            "original_name": pending.original_name,
            "started_at": pending.started_at,
            "updated_at": pending.updated_at,
            "pdf_sha256": pending.pdf_sha256,
            "pdf_size_bytes": pending.pdf_size_bytes,
            "has_staged_pdf": pending.has_staged_pdf,
            "previous_cv_version_id": pending.previous_cv_version_id,
            "reference_sha256": pending.reference_sha256,
            "warnings": list(pending.warnings),
            "error_message": pending.error_message,
        }
        _atomic_write_bytes(
            self._settings.cv_pending_path,
            _json_bytes(record),
        )

    def _write_pending_recovery(
        self,
        attempt_id: str,
        previous_cv_version_id: str | None,
    ) -> None:
        record: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "previous_cv_version_id": previous_cv_version_id,
        }
        _atomic_write_bytes(
            self._settings.cv_pending_recovery_path,
            _json_bytes(record),
        )

    def _load_pending_recovery(self) -> tuple[str, str | None]:
        record = self._read_json(
            self._settings.cv_pending_recovery_path,
            CvCorruptPendingError,
            "The pending CV recovery marker is unreadable.",
        )
        try:
            if record.get("schema_version") != _SCHEMA_VERSION:
                raise CvCorruptPendingError(
                    "The pending CV recovery marker uses an unsupported schema."
                )
            attempt_id = _mapping_string(record, "attempt_id")
            _validate_identifier(attempt_id, "CV import attempt")
            previous_cv_version_id = _optional_mapping_string(
                record,
                "previous_cv_version_id",
            )
            if previous_cv_version_id is not None:
                _validate_identifier(
                    previous_cv_version_id,
                    "previous CV version",
                )
            return attempt_id, previous_cv_version_id
        except CvValidationError as error:
            raise CvCorruptPendingError(
                "The pending CV recovery marker failed validation."
            ) from error

    def _load_version(self, version_id: str) -> CvVersionBundle:
        _validate_identifier(version_id, "CV version")
        version_path = self._settings.cv_versions_dir / version_id
        try:
            if version_path.resolve().parent != self._settings.cv_versions_dir.resolve():
                raise CvCorruptVersionError(
                    "The CV version points outside private version storage."
                )
        except OSError as error:
            raise CvCorruptVersionError(
                "The CV version path could not be verified."
            ) from error

        metadata = self._read_json(
            version_path / "metadata.json",
            CvCorruptVersionError,
            "The confirmed CV metadata is missing or unreadable.",
        )
        try:
            if metadata.get("schema_version") != _SCHEMA_VERSION:
                raise CvCorruptVersionError(
                    "The confirmed CV metadata uses an unsupported schema."
                )
            recorded_id = _mapping_string(metadata, "version_id")
            if recorded_id != version_id:
                raise CvCorruptVersionError(
                    "The confirmed CV metadata has a mismatched version identifier."
                )
            confirmed_at = _validated_timestamp(
                _mapping_string(metadata, "confirmed_at")
            )
            original_name = _mapping_string(metadata, "original_name")
            if _safe_pdf_basename(original_name) != original_name:
                raise CvCorruptVersionError(
                    "The confirmed CV metadata has an invalid original filename."
                )
            pdf_hash = _mapping_string(metadata, "pdf_sha256")
            reference_hash = _mapping_string(metadata, "cv_reference_hash")
            _validate_sha256(pdf_hash, "confirmed CV PDF")
            _validate_sha256(reference_hash, "confirmed CV reference")
            pdf_size = _mapping_integer(metadata, "pdf_size_bytes")
            warnings = _warnings_from_json(metadata.get("warnings"))
            try:
                pdf_bytes = (version_path / "cv.pdf").read_bytes()
            except OSError as error:
                raise CvCorruptVersionError(
                    "The confirmed CV PDF is missing or unreadable."
                ) from error
            self._validate_pdf_bytes(pdf_bytes)
            if (
                len(pdf_bytes) != pdf_size
                or hashlib.sha256(pdf_bytes).hexdigest() != pdf_hash
            ):
                raise CvCorruptVersionError(
                    "The confirmed CV PDF no longer matches its metadata."
                )
            reference = self._read_verified_reference(
                version_path / "reference.md",
                reference_hash,
                CvCorruptVersionError,
            )
            return CvVersionBundle(
                cv_version_id=version_id,
                confirmed_at=confirmed_at,
                original_name=original_name,
                pdf_sha256=pdf_hash,
                pdf_size_bytes=pdf_size,
                reference_markdown=reference,
                cv_reference_hash=reference_hash,
                warnings=warnings,
            )
        except CvValidationError as error:
            raise CvCorruptVersionError(
                "The confirmed CV bundle failed validation."
            ) from error

    def _select_previous_version(
        self,
        previous_version_id: str | None,
        *,
        pending_status: str,
    ) -> CvGenerationSelection:
        if previous_version_id is None:
            raise CvFallbackUnavailableError(
                "No verified previous CV can be identified for this upload "
                "problem. Complete or discard the pending CV import before "
                "generating a letter."
            )
        try:
            previous = self._load_version(previous_version_id)
        except CvCorruptVersionError as error:
            raise CvFallbackUnavailableError(
                "The previous confirmed CV is no longer valid. Complete the "
                "pending CV import before generating a letter."
            ) from error
        fallback_warning = (
            "This application explicitly uses the previous confirmed CV "
            f"because a newer CV import is {pending_status}."
        )
        return CvGenerationSelection(
            cv_version_id=previous.cv_version_id,
            reference_markdown=previous.reference_markdown,
            cv_reference_hash=previous.cv_reference_hash,
            used_previous_cv=True,
            warnings=(*previous.warnings, fallback_warning),
        )

    def _recover_pending_identity(self) -> tuple[str | None, str | None]:
        try:
            return self._load_pending_recovery()
        except CvCorruptPendingError:
            pass
        try:
            text = self._settings.cv_pending_path.read_text(encoding="utf-8")
            value = json.loads(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, None
        if not isinstance(value, dict):
            return None, None
        attempt_id = value.get("attempt_id")
        previous_version_id = value.get("previous_cv_version_id")
        try:
            _validate_identifier(attempt_id, "CV import attempt")
        except CvValidationError:
            attempt_id = None
        if previous_version_id is not None:
            try:
                _validate_identifier(previous_version_id, "previous CV version")
            except CvValidationError:
                previous_version_id = None
        return attempt_id, previous_version_id

    def _remove_pending_stage(self, attempt_id: str) -> None:
        path = self._pending_directory(attempt_id)
        if path.exists():
            shutil.rmtree(path)

    def _read_verified_reference(
        self,
        path: Path,
        expected_hash: str,
        error_type: type[CvImportError],
    ) -> str:
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise error_type(
                "The CV reference is missing or unreadable."
            ) from error
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise error_type("The CV reference is not valid UTF-8.") from error
        try:
            canonical = _canonicalize_reference(text)
        except CvValidationError as error:
            raise error_type(
                "The CV reference is not valid reviewed Markdown."
            ) from error
        if canonical.encode("utf-8") != raw:
            raise error_type(
                "The CV reference is not stored in canonical UTF-8/LF form."
            )
        if compute_cv_reference_hash(canonical) != expected_hash:
            raise error_type(
                "The CV reference no longer matches its recorded hash."
            )
        return canonical

    def _verified_pending_pdf_path(self, pending: CvPendingImport) -> Path:
        if (
            not pending.has_staged_pdf
            or pending.pdf_sha256 is None
            or pending.pdf_size_bytes <= 0
        ):
            raise CvCorruptPendingError(
                "The pending CV has no valid staged PDF."
            )
        path = self._pending_directory(pending.attempt_id) / "cv.pdf"
        try:
            data = path.read_bytes()
        except OSError as error:
            raise CvCorruptPendingError(
                "The staged CV PDF is missing or unreadable."
            ) from error
        try:
            self._validate_pdf_bytes(data)
        except CvValidationError as error:
            raise CvCorruptPendingError(
                "The staged CV PDF failed validation."
            ) from error
        if (
            len(data) != pending.pdf_size_bytes
            or hashlib.sha256(data).hexdigest() != pending.pdf_sha256
        ):
            raise CvCorruptPendingError(
                "The staged CV PDF no longer matches its pending record."
            )
        return path

    def _pending_directory(self, attempt_id: str) -> Path:
        _validate_identifier(attempt_id, "CV import attempt")
        path = self._settings.cv_staging_dir / attempt_id
        try:
            if path.resolve().parent != self._settings.cv_staging_dir.resolve():
                raise CvCorruptPendingError(
                    "The pending CV path points outside private staging."
                )
        except OSError as error:
            raise CvCorruptPendingError(
                "The pending CV path could not be verified."
            ) from error
        return path

    def _require_pending(self) -> CvPendingImport:
        pending = self.load_pending()
        if pending is None:
            raise CvPendingNotFoundError(
                "There is no pending CV import. Upload a PDF first."
            )
        return pending

    def _validate_pdf_bytes(self, file_bytes: bytes) -> None:
        if type(file_bytes) is not bytes:
            raise CvValidationError("The uploaded CV must be provided as bytes.")
        if not file_bytes:
            raise CvValidationError("The uploaded CV PDF is empty.")
        if len(file_bytes) > self._settings.max_cv_pdf_bytes:
            raise CvValidationError(
                "The uploaded CV PDF exceeds the configured 25 MiB size limit."
            )
        if not file_bytes.startswith(b"%PDF-"):
            raise CvValidationError(
                "The uploaded file does not have a valid PDF header."
            )

    def _read_json(
        self,
        path: Path,
        error_type: type[CvImportError],
        message: str,
    ) -> Mapping[str, object]:
        try:
            text = path.read_text(encoding="utf-8")
            value = json.loads(text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise error_type(message) from error
        if not isinstance(value, dict):
            raise error_type(message)
        return value

    def _new_identifier(self, label: str) -> str:
        identifier = self._id_factory()
        try:
            _validate_identifier(identifier, label)
        except CvValidationError as error:
            raise CvPublicationError(
                "Could not create a safe private CV identifier. Retry the operation."
            ) from error
        return identifier

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    def _ensure_directories(self) -> None:
        try:
            self._settings.cv_dir.mkdir(parents=True, exist_ok=True)
            self._settings.cv_versions_dir.mkdir(parents=True, exist_ok=True)
            self._settings.cv_staging_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            LOGGER.warning("Private CV directory creation failed")
            raise CvPublicationError(
                "Could not create private CV storage. Check data directory "
                "permissions and try again."
            ) from error


def _canonicalize_reference(text: str) -> str:
    if not isinstance(text, str):
        raise CvValidationError("The CV reference must be text.")
    canonical = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    invalid_controls = [
        character
        for character in canonical
        if ord(character) < 32 and character not in ("\t", "\n")
    ]
    if invalid_controls:
        raise CvValidationError(
            "The CV reference contains unsupported control characters."
        )
    canonical = canonical.rstrip("\n") + "\n"
    if not canonical.startswith("# Profile\n"):
        raise CvValidationError(
            "The CV reference must begin with the '# Profile' heading."
        )
    if not canonical[len("# Profile\n") :].strip():
        raise CvValidationError(
            "The CV reference must contain reviewable profile details."
        )
    try:
        encoded = canonical.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise CvValidationError("The CV reference is not valid UTF-8 text.") from error
    if len(encoded) > _MAX_REFERENCE_BYTES:
        raise CvValidationError("The CV reference exceeds the safe text size limit.")
    return canonical


def _safe_pdf_basename(original_name: str) -> str:
    if not isinstance(original_name, str):
        raise CvValidationError("The uploaded CV filename must be text.")
    basename = original_name.replace("\\", "/").rsplit("/", maxsplit=1)[-1].strip()
    if len(basename) < 5 or basename[-4:].casefold() != ".pdf":
        raise CvValidationError("The uploaded CV must have a .PDF extension.")
    suffix = basename[-4:]
    stem = basename[:-4]
    stem = _WINDOWS_UNSAFE_NAME_PATTERN.sub("", stem)
    stem = _WHITESPACE_PATTERN.sub(" ", stem).strip(" .")
    if not stem:
        stem = "cv"
    stem = stem[:120].rstrip(" .") or "cv"
    if stem.casefold() in _WINDOWS_RESERVED_STEMS:
        stem = f"cv-{stem}"
    return stem + suffix


def _validate_identifier(identifier: object, label: str) -> None:
    if not isinstance(identifier, str) or _IDENTIFIER_PATTERN.fullmatch(
        identifier
    ) is None:
        raise CvValidationError(f"The {label} identifier is invalid.")


def _validate_sha256(value: object, label: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise CvValidationError(f"The {label} hash is invalid.")


def _validate_warnings(warnings: object) -> tuple[str, ...]:
    if not isinstance(warnings, tuple):
        raise CvValidationError("CV warnings must be provided as a tuple.")
    normalized: list[str] = []
    for warning in warnings:
        if not isinstance(warning, str) or not warning.strip():
            raise CvValidationError("Every CV warning must be non-blank text.")
        if any(
            ord(character) < 32 and character not in ("\t", "\n")
            for character in warning
        ):
            raise CvValidationError(
                "CV warnings contain unsupported control characters."
            )
        normalized.append(warning.strip())
    return tuple(normalized)


def _warnings_from_json(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise CvValidationError("Persisted CV warnings must be a list.")
    return _validate_warnings(tuple(value))


def _mapping_string(record: Mapping[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise CvValidationError(f"Persisted CV field {key} must be non-blank text.")
    return value


def _optional_mapping_string(
    record: Mapping[str, object],
    key: str,
) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CvValidationError(
            f"Persisted CV field {key} must be text or null."
        )
    return value


def _mapping_integer(record: Mapping[str, object], key: str) -> int:
    value = record.get(key)
    if type(value) is not int:
        raise CvValidationError(f"Persisted CV field {key} must be an integer.")
    return value


def _mapping_boolean(record: Mapping[str, object], key: str) -> bool:
    value = record.get(key)
    if type(value) is not bool:
        raise CvValidationError(f"Persisted CV field {key} must be a boolean.")
    return value


def _validated_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise CvValidationError("Persisted CV timestamp is invalid.") from error
    if parsed.tzinfo is None:
        raise CvValidationError("Persisted CV timestamp must include a timezone.")
    return value


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        _write_new_bytes(temporary, data)
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Private CV temporary-file cleanup failed")
        raise


def _write_new_bytes(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _remove_tree_quietly(path: Path, expected_parent: Path) -> None:
    try:
        resolved_path = path.resolve()
        resolved_parent = expected_parent.resolve()
        if resolved_path.parent != resolved_parent:
            LOGGER.warning("Refused unsafe private CV cleanup")
            return
        if resolved_path.exists():
            shutil.rmtree(resolved_path)
    except OSError:
        LOGGER.warning("Private CV temporary-directory cleanup failed")
