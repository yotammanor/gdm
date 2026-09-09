"""Prove explore/infer/doctor/migration dry-run make no content API calls."""

from __future__ import annotations

from gdm import Field, GDMModel
from gdm.collection.metadata import InMemoryCollectionMetadataStore
from gdm.explore import Explorer, InMemoryDriveService
from gdm.infer import infer_from_collection_inventory
from gdm.migrations import (
    InMemoryMigrationTarget,
    InMemorySheet,
    MigrationEngine,
    MigrationPlanner,
)
from gdm.schema import build_model_snapshot
from gdm.tooling.doctor import run_collection_doctor


class Asset(GDMModel, storage="collection", collection="Assets", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")
    tag: str | None = Field(default=None, metadata="tag")


class AssetV2(GDMModel, storage="collection", collection="Assets", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")
    tag: str | None = Field(default=None, metadata="tag")
    status: str | None = Field(default=None, metadata="status")


def test_explore_infer_doctor_migration_dry_run_no_forbidden_calls() -> None:
    service = InMemoryDriveService(
        children={
            "folder": [
                {"id": "1", "name": "a", "mimeType": "image/png"},
            ]
        }
    )
    explorer = Explorer(service)
    report = explorer.explore("folder")
    infer_from_collection_inventory(
        report.inventory.resources, folder_id="folder", model_name="Asset"
    )
    store = InMemoryCollectionMetadataStore(rows={})
    run_collection_doctor(
        model_name="Asset",
        folder_id="folder",
        folder_file_ids=["1"],
        mime_types=["image/png"],
        metadata_store=store,
    )
    target = InMemoryMigrationTarget(
        sheets={},
        model_bindings={"Asset": "folder"},
        files={},
        indexes={},
        metadata_sheets={"Asset": InMemorySheet(headers=["tag"], rows=[])},
    )
    applied = build_model_snapshot(Asset)
    current = build_model_snapshot(AssetV2)
    plan = MigrationPlanner().plan_from_snapshots(
        model_name="Asset",
        current=applied,
        target=current,
        resource_id="folder",
        dry_run=True,
    )
    MigrationEngine(target).apply(plan)
    joined = " ".join(service.call_log + target.write_log)
    for forbidden in (
        "media",
        "export",
        "documents",
        "presentations",
        "spreadsheets.values",
    ):
        assert forbidden not in joined
