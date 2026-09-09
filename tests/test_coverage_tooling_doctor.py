"""Branch coverage for tooling helpers: doctor, adopt, mime, probes."""

from __future__ import annotations

from gdm.collection.constants import DOC_MIME, SHEET_MIME, SLIDE_MIME
from gdm.collection.metadata import (
    CollectionMetadataRow,
    InMemoryCollectionMetadataStore,
)
from gdm.explore_types import DriveResource, ResourceKind
from gdm.schema import DriftDiagnostic, DriftReport, DriftSeverity, detect_drift
from gdm.tooling.adopt import AdoptPlanner
from gdm.tooling.collection_probe import collection_boundaries
from gdm.tooling.doctor import (
    CollectionDoctorReport,
    DoctorIssueKind,
    content_capabilities_for_mime,
    doctor_payload,
    run_collection_doctor,
)
from gdm.tooling.mime import MimeCategory, categorize_mime, mime_selector_for_category


def test_mime_categories_and_selectors() -> None:
    assert categorize_mime(SLIDE_MIME) is MimeCategory.SLIDE
    assert categorize_mime(DOC_MIME) is MimeCategory.DOC
    assert categorize_mime(SHEET_MIME) is MimeCategory.SHEET
    assert categorize_mime("application/vnd.google-apps.form") is MimeCategory.OTHER
    assert categorize_mime("text/plain") is MimeCategory.OTHER
    assert categorize_mime("application/json") is MimeCategory.OTHER
    assert categorize_mime("application/octet-stream") is MimeCategory.BINARY
    assert mime_selector_for_category(MimeCategory.IMAGE) == "image/*"
    assert mime_selector_for_category(MimeCategory.OTHER) is None


def test_content_capabilities_by_mime() -> None:
    assert "google_doc" in content_capabilities_for_mime(DOC_MIME)
    assert "google_slides" in content_capabilities_for_mime(SLIDE_MIME)
    assert "google_sheet" in content_capabilities_for_mime(SHEET_MIME)
    assert "export" in content_capabilities_for_mime("application/vnd.google-apps.form")
    caps = content_capabilities_for_mime("application/zip")
    assert "download" in caps


class _MetadataStore:
    def reconcile(self, *, folder_file_ids: set[str]) -> tuple[list[str], list[str]]:
        return ["orphan"], ["missing"]


def test_doctor_orphans_stale_indexes_and_drift() -> None:
    drift = DriftReport(
        model_snapshot=None,
        applied_snapshot=None,
        live_schema=None,
        diagnostics=(
            DriftDiagnostic(
                code="bad",
                message="schema mismatch",
                severity=DriftSeverity.ERROR,
                action="fix",
            ),
        ),
    )
    report = run_collection_doctor(
        model_name="Asset",
        folder_id="f1",
        folder_file_ids=["keep"],
        mime_types=["image/png", "application/pdf"],
        metadata_store=_MetadataStore(),
        drift_report=drift,
        indexed_fields=["tag"],
        stale_indexes=["email"],
    )
    assert report.error_count >= 1
    assert "orphan" in report.orphan_metadata_rows
    assert "missing" in report.missing_metadata_rows
    kinds = {issue.kind for issue in report.issues}
    assert DoctorIssueKind.INDEX in kinds
    assert DoctorIssueKind.DRIVE_METADATA in kinds
    payload = doctor_payload(report)
    assert payload["error_count"] >= 1
    assert payload["issues"]


def test_doctor_without_metadata_store() -> None:
    report = run_collection_doctor(
        model_name="Asset",
        folder_id="f1",
        folder_file_ids=[],
        mime_types=[],
    )
    assert isinstance(report, CollectionDoctorReport)
    assert report.issues


def test_doctor_warns_for_scale_duplicates_and_unindexed_scans() -> None:
    report = run_collection_doctor(
        model_name="Asset",
        folder_id="f1",
        folder_file_ids=["1", "2"],
        folder_names=["duplicate", "duplicate"],
        mime_types=["image/png", "image/png"],
        large_collection_threshold=2,
    )
    codes = {issue.code for issue in report.issues}
    assert {"bounded_scan_only", "duplicate_names", "large_collection"} <= codes


def test_adopt_apply_without_binder() -> None:
    planner = AdoptPlanner()
    plan = planner.plan_bind_folder(model_name="Asset", folder_id="f1")
    result = planner.apply_plan(plan, dry_run=False, binder=None)
    assert not result.applied
    assert "No binder" in result.message


def test_adopt_apply_with_binder() -> None:
    planner = AdoptPlanner()
    plan = planner.plan_bind_folder(model_name="Asset", folder_id="f1")
    result = planner.apply_plan(
        plan,
        dry_run=False,
        binder=lambda p: {"folder_id": p.folder_id, "model": p.model_name},
    )
    assert result.applied
    assert result.binding is not None
    assert result.binding["folder_id"] == "f1"


def test_collection_boundaries() -> None:
    resources = (
        DriveResource(
            "root",
            "Root",
            "application/vnd.google-apps.folder",
            ResourceKind.FOLDER,
            None,
        ),
        DriveResource(
            "f1",
            "Assets",
            "application/vnd.google-apps.folder",
            ResourceKind.FOLDER,
            "root",
            path=("Assets",),
        ),
        DriveResource(
            "a1",
            "photo.png",
            "image/png",
            ResourceKind.FILE,
            "f1",
        ),
    )
    bounds = collection_boundaries(resources)
    asset = next(b for b in bounds if b.folder_id == "f1")
    assert asset.direct_child_count == 1


def test_inmemory_metadata_reconcile() -> None:
    store = InMemoryCollectionMetadataStore(
        rows={
            "keep": CollectionMetadataRow(file_id="keep", values={"tag": "a"}),
            "orphan": CollectionMetadataRow(file_id="orphan", values={"tag": "b"}),
        }
    )
    orphan, missing = store.reconcile(folder_file_ids={"keep", "new"})
    assert "orphan" in orphan
    assert "new" in missing


def test_doctor_payload_structure() -> None:
    report = run_collection_doctor(
        model_name="M",
        folder_id="f",
        folder_file_ids=[],
        mime_types=["image/png"],
    )
    payload = doctor_payload(report)
    assert set(payload) >= {"model_name", "folder_id", "issues", "content_capabilities"}


def test_drift_report_has_errors_used_by_doctor() -> None:
    drift = detect_drift(model_snapshot=None, applied_snapshot=None, live_schema=None)
    assert drift.has_errors is False
