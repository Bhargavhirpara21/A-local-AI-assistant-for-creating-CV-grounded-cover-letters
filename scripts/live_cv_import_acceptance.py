"""Run an authorized, content-free live acceptance of one private CV import."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from config import Settings, build_settings
from core.cv_import import (
    CvExtractionError,
    CvImportError,
    CvImportWorkflow,
    CvPendingExistsError,
    CvPendingImport,
)
from core.source_library import SourceLibrary, SourceLibraryError
from llm import get_client
from llm.base import LLMClient

LOGGER = logging.getLogger(__name__)

_READ_CHUNK_BYTES = 1024 * 1024
_CLOUD_FILE_WINERRORS: frozenset[int] = frozenset(
    {
        358,
        362,
        363,
        364,
        365,
        366,
        374,
        375,
        377,
        379,
        380,
        381,
        382,
        383,
        386,
        387,
        388,
        389,
        390,
        391,
        393,
        394,
        395,
        397,
        398,
        404,
        426,
        434,
        4350,
    }
)

SettingsBuilder = Callable[[], Settings]
ClientBuilder = Callable[[Settings], LLMClient]
SourceBuilder = Callable[[Settings], SourceLibrary]
WorkflowBuilder = Callable[[Settings, LLMClient], CvImportWorkflow]
PdfReader = Callable[[Path, int], bytes]


class CvAcceptanceError(RuntimeError):
    """Base error for the privacy-safe live CV acceptance command."""


class CvAcceptanceConsentError(CvAcceptanceError):
    """Raised before any private access when explicit consent is absent."""


class CvAcceptanceEnvironmentError(CvAcceptanceError):
    """Raised when the configured backend or authentication mode is unsafe."""


class CvExternalPdfAccessError(CvAcceptanceError):
    """Raised when Windows cannot safely provide the selected external PDF."""


class CvAcceptanceWorkflowError(CvAcceptanceError):
    """Raised when staging or extraction cannot produce a review draft."""


class CvAcceptanceSourceChangedError(CvAcceptanceError):
    """Raised when the managed source library changes during CV acceptance."""


@dataclass(frozen=True, slots=True)
class SourceHashSnapshot:
    """Opaque DE/EN source hashes retained without exposing them in repr/logs."""

    de_sha256: str = field(repr=False)
    en_sha256: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CvAcceptanceResult:
    """Non-content acceptance receipt for a private pending review draft."""

    attempt_id: str
    pending_status: str
    pdf_size_bytes: int
    source_hashes_before: SourceHashSnapshot = field(repr=False)
    source_hashes_after: SourceHashSnapshot = field(repr=False)

    @property
    def source_hashes_unchanged(self) -> bool:
        """Return whether both managed language bundles stayed unchanged."""

        return self.source_hashes_before == self.source_hashes_after


def run_acceptance(
    pdf_path: Path,
    *,
    confirmed: bool,
    environment: Mapping[str, str] | None = None,
    settings_builder: SettingsBuilder | None = None,
    client_builder: ClientBuilder | None = None,
    source_builder: SourceBuilder | None = None,
    workflow_builder: WorkflowBuilder | None = None,
    pdf_reader: PdfReader | None = None,
) -> CvAcceptanceResult:
    """Import one CV into review state after explicit remote-transfer consent."""

    if confirmed is not True:
        raise CvAcceptanceConsentError(
            "Explicit consent is required before reading or sending the private "
            "CV PDF to Claude."
        )

    selected_environment = os.environ if environment is None else environment
    selected_settings_builder = settings_builder or build_settings
    settings = selected_settings_builder()
    if settings.backend != "agent_sdk":
        raise CvAcceptanceEnvironmentError(
            "The live CV acceptance gate requires the Agent SDK subscription "
            "backend."
        )
    api_key = selected_environment.get("ANTHROPIC_API_KEY")
    if api_key is not None and api_key.strip():
        raise CvAcceptanceEnvironmentError(
            "Unset ANTHROPIC_API_KEY before this acceptance gate so Claude uses "
            "the logged-in subscription."
        )

    selected_source_builder = source_builder or SourceLibrary
    sources = selected_source_builder(settings)
    if not sources.is_ready():
        raise CvAcceptanceWorkflowError(
            "The managed source library must be complete before CV acceptance."
        )
    before = _source_hash_snapshot(sources)
    LOGGER.info("Authorized private CV acceptance started")

    operation_error: (
        CvAcceptanceError | CvImportError | ImportError | ValueError | None
    ) = None
    review: CvPendingImport | None = None
    try:
        selected_reader = pdf_reader or _read_external_pdf
        file_bytes = selected_reader(pdf_path, settings.max_cv_pdf_bytes)
        selected_client_builder = client_builder or get_client
        try:
            client = selected_client_builder(settings)
        except (ImportError, ValueError) as error:
            operation_error = error
        else:
            selected_workflow_builder = workflow_builder or CvImportWorkflow
            workflow = selected_workflow_builder(settings, client)
            try:
                workflow.start_import(file_bytes, pdf_path.name)
                extracted = workflow.extract_pending()
                loaded = workflow.load_pending()
                if (
                    extracted.status != "review"
                    or loaded is None
                    or loaded.attempt_id != extracted.attempt_id
                    or loaded.status != "review"
                    or loaded.reference_markdown is None
                    or loaded.reference_sha256 is None
                ):
                    raise CvAcceptanceWorkflowError(
                        "CV extraction did not produce a durable review draft."
                    )
                review = loaded
            except (CvAcceptanceError, CvImportError) as error:
                operation_error = error
    except CvAcceptanceError as error:
        operation_error = error

    after = _source_hash_snapshot(sources)
    if before != after:
        raise CvAcceptanceSourceChangedError(
            "The managed source library changed during CV acceptance. The "
            "pending CV remains inactive for review."
        )
    if operation_error is not None:
        raise _safe_workflow_error(operation_error) from operation_error
    if review is None:
        raise CvAcceptanceWorkflowError(
            "CV acceptance ended without a durable review draft."
        )

    LOGGER.info("Managed source hashes remained unchanged")
    LOGGER.info("Private CV draft is awaiting human review")
    return CvAcceptanceResult(
        attempt_id=review.attempt_id,
        pending_status=review.status,
        pdf_size_bytes=review.pdf_size_bytes,
        source_hashes_before=before,
        source_hashes_after=after,
    )


def main(pdf_path: Path, confirmed: bool) -> int:
    """Run the command and return a process exit code with safe diagnostics."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        run_acceptance(pdf_path, confirmed=confirmed)
    except CvAcceptanceError as error:
        LOGGER.error("%s", error)
        return 1
    return 0


def _source_hash_snapshot(sources: SourceLibrary) -> SourceHashSnapshot:
    try:
        return SourceHashSnapshot(
            de_sha256=sources.load_bundle("de").sha256,
            en_sha256=sources.load_bundle("en").sha256,
        )
    except SourceLibraryError as error:
        raise CvAcceptanceWorkflowError(
            "The managed source hashes could not be read consistently."
        ) from error


def _safe_workflow_error(
    error: CvAcceptanceError | CvImportError | ImportError | ValueError,
) -> CvAcceptanceError:
    if isinstance(error, CvAcceptanceError):
        return error
    if isinstance(error, CvPendingExistsError):
        return CvAcceptanceWorkflowError(
            "A CV import is already pending. Review, retry, confirm, or discard "
            "it before starting another acceptance."
        )
    if isinstance(error, CvExtractionError):
        return CvAcceptanceWorkflowError(
            "Claude could not prepare the CV review draft. The failed pending "
            "attempt was kept and was not retried automatically."
        )
    if isinstance(error, (ImportError, ValueError)):
        return CvAcceptanceWorkflowError(
            "The Agent SDK client could not be initialized. Reinstall the "
            "project dependencies before retrying."
        )
    return CvAcceptanceWorkflowError(
        "The CV acceptance workflow could not prepare a review draft. No CV "
        "version was activated."
    )


def _read_external_pdf(pdf_path: Path, maximum_bytes: int) -> bytes:
    """Read one external PDF within a fixed limit and map Windows failures."""

    if maximum_bytes <= 0:
        raise CvExternalPdfAccessError(
            "The configured CV size limit is invalid."
        )
    data = bytearray()
    try:
        with pdf_path.open("rb") as stream:
            while True:
                remaining = maximum_bytes - len(data)
                chunk = stream.read(min(_READ_CHUNK_BYTES, remaining + 1))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > maximum_bytes:
                    raise CvExternalPdfAccessError(
                        "The selected CV PDF exceeds the configured size limit."
                    )
    except CvExternalPdfAccessError:
        raise
    except OSError as error:
        raise _external_access_error(error) from error
    return bytes(data)


def _external_access_error(error: OSError) -> CvExternalPdfAccessError:
    winerror = getattr(error, "winerror", None)
    lowered = str(error).casefold()
    if winerror in (32, 33):
        return CvExternalPdfAccessError(
            "The CV PDF is locked by another program. Close the file and try "
            "again."
        )
    if winerror == 5:
        return CvExternalPdfAccessError(
            "Windows denied permission to read the CV PDF. Check its access "
            "permissions or copy it to a readable local folder."
        )
    if (
        winerror in _CLOUD_FILE_WINERRORS
        or "cloud" in lowered
        or "onedrive" in lowered
        or "offline" in lowered
    ):
        return CvExternalPdfAccessError(
            "The OneDrive CV PDF is not locally available. Make the file "
            "available offline, then try again."
        )
    if isinstance(error, FileNotFoundError):
        return CvExternalPdfAccessError(
            "The selected CV PDF no longer exists or is unavailable."
        )
    if isinstance(error, IsADirectoryError):
        return CvExternalPdfAccessError(
            "The selected CV location is a folder, not a PDF file."
        )
    return CvExternalPdfAccessError(
        "Windows could not read the selected CV PDF. Check access and try again."
    )


def _parse_arguments() -> tuple[Path, bool]:
    parser = argparse.ArgumentParser(
        description=(
            "Import one private CV into an inactive review draft after consent."
        )
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        required=True,
        help="Path to the CV PDF; the path itself is never logged.",
    )
    parser.add_argument(
        "--confirm-private-cv-pdf-transmission-to-claude",
        action="store_true",
        required=True,
        help="Confirm that the raw CV PDF may be sent to Claude for extraction.",
    )
    arguments = parser.parse_args()
    return (
        arguments.pdf,
        bool(arguments.confirm_private_cv_pdf_transmission_to_claude),
    )


if __name__ == "__main__":
    selected_pdf, consent = _parse_arguments()
    raise SystemExit(main(selected_pdf, consent))
