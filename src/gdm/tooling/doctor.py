"""Collection doctor: membership, metadata, orphan, and capability checks."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from gdm.collection.constants import (
    DOC_MIME,
    READ_ONLY_DRIVE_FIELDS,
    SHEET_MIME,
    SLIDE_MIME,
)
from gdm.schema import DriftReport


class DoctorIssueKind(StrEnum):
    """Categories of collection doctor findings."""

    MEMBERSHIP = "membership"
    DRIVE_METADATA = "drive_metadata"
    METADATA_TABLE = "metadata_table"
    CONTENT_CAPABILITY = "content_capability"
    INDEX = "index"
    PROJECT = "project"
    QUERY = "query"
    SCALE = "scale"


@dataclass(frozen=True, slots=True)
class DoctorIssue:
    """One doctor finding."""

    kind: DoctorIssueKind
    code: str
    message: str
    severity: str = "info"
    action: str | None = None


@dataclass
class CollectionDoctorReport:
    """Aggregated doctor output for a collection binding."""

    model_name: str
    folder_id: str
    issues: tuple[DoctorIssue, ...] = field(default_factory=tuple)
    drift: DriftReport | None = None
    content_capabilities: tuple[str, ...] = field(default_factory=tuple)
    orphan_metadata_rows: tuple[str, ...] = field(default_factory=tuple)
    missing_metadata_rows: tuple[str, ...] = field(default_factory=tuple)

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error")


@runtime_checkable
class MetadataReconciler(Protocol):
    """Protocol for metadata orphan/missing reconciliation."""

    def reconcile(
        self,
        *,
        folder_file_ids: set[str],
    ) -> tuple[list[str], list[str]]: ...


@runtime_checkable
class CapabilityInspector(Protocol):
    """Protocol for capability inspection without content reads."""

    def capabilities_for_mime(self, mime_type: str) -> Mapping[str, bool]: ...


def content_capabilities_for_mime(mime_type: str) -> tuple[str, ...]:
    """Return supported native content operations for a MIME type."""
    if mime_type == DOC_MIME:
        return ("google_doc", "export")
    if mime_type == SLIDE_MIME:
        return ("google_slides", "export")
    if mime_type == SHEET_MIME:
        return ("google_sheet", "export")
    if mime_type.startswith("image/") or mime_type == "application/pdf":
        return ("download", "upload", "export")
    if mime_type.startswith("application/vnd.google-apps."):
        return ("export",)
    return ("download", "upload")


def run_collection_doctor(
    *,
    model_name: str,
    folder_id: str,
    folder_file_ids: Sequence[str],
    mime_types: Sequence[str],
    folder_names: Sequence[str] | None = None,
    metadata_store: MetadataReconciler | None = None,
    drift_report: DriftReport | None = None,
    indexed_fields: Sequence[str] | None = None,
    stale_indexes: Sequence[str] | None = None,
    large_collection_threshold: int = 1000,
) -> CollectionDoctorReport:
    """Reconcile collection membership and metadata without reading file bodies."""
    issues: list[DoctorIssue] = []
    orphan_rows: tuple[str, ...] = ()
    missing_rows: tuple[str, ...] = ()

    if metadata_store is not None:
        orphan, missing = metadata_store.reconcile(
            folder_file_ids=set(folder_file_ids),
        )
        orphan_rows = tuple(orphan)
        missing_rows = tuple(missing)
        for row_id in orphan_rows:
            issues.append(
                DoctorIssue(
                    kind=DoctorIssueKind.METADATA_TABLE,
                    code="orphan_metadata_row",
                    message=(
                        f"Metadata row for Drive ID {row_id!r} has no folder member."
                    ),
                    severity="warning",
                    action="Remove orphan row or restore the Drive file.",
                )
            )
        for row_id in missing_rows:
            issues.append(
                DoctorIssue(
                    kind=DoctorIssueKind.METADATA_TABLE,
                    code="missing_metadata_row",
                    message=f"Folder member {row_id!r} has no metadata-table row.",
                    severity="info",
                    action="Backfill default metadata on next read or migration.",
                )
            )

    caps: set[str] = set()
    for mime in mime_types:
        caps.update(content_capabilities_for_mime(mime))

    if len(folder_file_ids) >= large_collection_threshold:
        issues.append(
            DoctorIssue(
                kind=DoctorIssueKind.SCALE,
                code="large_collection",
                message=(
                    f"Collection has at least {len(folder_file_ids)} direct members; "
                    f"the warning threshold is {large_collection_threshold}."
                ),
                severity="warning",
                action=(
                    "Use selective MIME filters, lower scan limits, or verified "
                    "sidecar indexes for frequent equality queries."
                ),
            )
        )

    if folder_names:
        duplicates = sorted(
            name for name, count in Counter(folder_names).items() if count > 1
        )
        if duplicates:
            preview = ", ".join(repr(name) for name in duplicates[:5])
            issues.append(
                DoctorIssue(
                    kind=DoctorIssueKind.MEMBERSHIP,
                    code="duplicate_names",
                    message=f"Duplicate direct-child names: {preview}.",
                    severity="warning",
                    action="Use Drive file IDs or one_by_name() ambiguity handling.",
                )
            )

    if stale_indexes:
        for field_name in stale_indexes:
            issues.append(
                DoctorIssue(
                    kind=DoctorIssueKind.INDEX,
                    code="stale_index",
                    message=f"Sidecar index for '{field_name}' is stale.",
                    severity="warning",
                    action="Run `gdm index rebuild` (metadata-only).",
                )
            )

    if indexed_fields:
        issues.append(
            DoctorIssue(
                kind=DoctorIssueKind.INDEX,
                code="indexed_fields",
                message=f"Indexed fields: {', '.join(indexed_fields)}.",
                severity="info",
            )
        )
    elif len(folder_file_ids) >= max(1, large_collection_threshold // 4):
        issues.append(
            DoctorIssue(
                kind=DoctorIssueKind.QUERY,
                code="bounded_scan_only",
                message=(
                    "No indexed fields are declared; metadata predicates require "
                    "bounded client-side scans."
                ),
                severity="warning",
                action="Add Field(index=True) for frequent equality filters.",
            )
        )

    issues.append(
        DoctorIssue(
            kind=DoctorIssueKind.CONTENT_CAPABILITY,
            code="readonly_drive_fields",
            message=(
                f"Read-only Drive fields: {', '.join(sorted(READ_ONLY_DRIVE_FIELDS))}."
            ),
            severity="info",
        )
    )

    if drift_report is not None and drift_report.has_errors:
        for diag in drift_report.diagnostics:
            if diag.severity.value == "error":
                issues.append(
                    DoctorIssue(
                        kind=DoctorIssueKind.DRIVE_METADATA,
                        code=diag.code,
                        message=diag.message,
                        severity="error",
                        action=diag.action,
                    )
                )

    return CollectionDoctorReport(
        model_name=model_name,
        folder_id=folder_id,
        issues=tuple(issues),
        drift=drift_report,
        content_capabilities=tuple(sorted(caps)),
        orphan_metadata_rows=orphan_rows,
        missing_metadata_rows=missing_rows,
    )


def doctor_payload(report: CollectionDoctorReport) -> dict[str, Any]:
    """Serialize a doctor report for CLI JSON output."""
    return {
        "content_capabilities": list(report.content_capabilities),
        "error_count": report.error_count,
        "folder_id": report.folder_id,
        "issues": [
            {
                "action": issue.action,
                "code": issue.code,
                "kind": issue.kind.value,
                "message": issue.message,
                "severity": issue.severity,
            }
            for issue in report.issues
        ],
        "missing_metadata_rows": list(report.missing_metadata_rows),
        "model_name": report.model_name,
        "orphan_metadata_rows": list(report.orphan_metadata_rows),
    }
