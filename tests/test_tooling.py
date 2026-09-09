"""Tests for collection tooling helpers."""

from __future__ import annotations

from gdm.collection.metadata import InMemoryCollectionMetadataStore
from gdm.explore import DriveResource, ResourceKind
from gdm.tooling.adopt import AdoptPlanner
from gdm.tooling.collection_probe import analyze_collection_candidates
from gdm.tooling.doctor import run_collection_doctor
from gdm.tooling.mime import MimeCategory, categorize_mime


def test_categorize_mime() -> None:
    assert categorize_mime("application/vnd.google-apps.folder") is MimeCategory.FOLDER
    assert categorize_mime("image/png") is MimeCategory.IMAGE
    assert categorize_mime("application/pdf") is MimeCategory.PDF


def test_collection_candidates_from_inventory() -> None:
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
            path=("Assets", "photo.png"),
        ),
        DriveResource(
            "a2",
            "photo.png",
            "image/png",
            ResourceKind.FILE,
            "f1",
            path=("Assets", "photo.png"),
        ),
    )
    candidates = analyze_collection_candidates(resources, root_id="root")
    asset = next(c for c in candidates if c.folder_id == "f1")
    assert asset.direct_child_count == 2
    assert asset.duplicate_name_groups == 1


def test_adopt_plan_defaults_to_dry_run() -> None:
    planner = AdoptPlanner()
    plan = planner.plan_bind_folder(model_name="Asset", folder_id="f1")
    result = planner.apply_plan(plan, dry_run=True)
    assert not result.applied
    assert "Dry-run" in result.message


def test_doctor_reconciles_orphans() -> None:
    store = InMemoryCollectionMetadataStore(
        rows={
            "keep": store_row("keep", {"tag": "a"}),
            "orphan": store_row("orphan", {"tag": "b"}),
        }
    )
    report = run_collection_doctor(
        model_name="Asset",
        folder_id="f1",
        folder_file_ids=["keep"],
        mime_types=["image/png"],
        metadata_store=store,
    )
    assert "orphan" in report.orphan_metadata_rows
    assert "download" in report.content_capabilities


def store_row(file_id: str, values: dict[str, str]):
    from gdm.collection.metadata import CollectionMetadataRow

    return CollectionMetadataRow(file_id=file_id, values=values)
