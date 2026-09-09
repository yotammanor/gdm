"""Collection inference and codegen tests."""

from __future__ import annotations

import json

from gdm.explore import DriveResource, Explorer, InMemoryDriveService, ResourceKind
from gdm.infer import (
    exec_generated_model,
    generate_model_code,
    infer_from_collection_inventory,
    infer_from_json_text,
)
from gdm.model import GDMModel


def _sample_inventory() -> tuple[DriveResource, ...]:
    return (
        DriveResource(
            "folder",
            "Assets",
            "application/vnd.google-apps.folder",
            ResourceKind.FOLDER,
            "root",
        ),
        DriveResource(
            "f1",
            "hero.png",
            "image/png",
            ResourceKind.FILE,
            "folder",
        ),
    )


def test_infer_collection_uses_drive_and_metadata_fields() -> None:
    result = infer_from_collection_inventory(
        _sample_inventory(),
        folder_id="folder",
        model_name="Asset",
        metadata_headers=["campaign", "approved"],
        metadata_rows=[["launch", "yes"]],
    )
    assert result.binding.storage == "collection"
    assert result.binding.primary_key == "drive_id"
    kinds = {c.target_kind for c in result.columns}
    assert "drive" in kinds
    assert "metadata" in kinds


def test_generated_collection_model_imports_and_executes() -> None:
    result = infer_from_collection_inventory(
        _sample_inventory(),
        folder_id="folder",
        model_name="Asset",
        collection_path="Assets",
    )
    code = generate_model_code(result)
    assert "gdm_config" not in code
    assert 'storage="collection"' in code
    assert 'Field(..., drive="id"' in code
    model = exec_generated_model(code, model_name="Asset")
    assert issubclass(model, GDMModel)
    assert model.gdm_storage() == "collection"


def test_infer_file_json_explicit_path() -> None:
    result = infer_from_json_text(json.dumps({"title": "x"}), model_name="Doc")
    code = generate_model_code(result)
    assert "class Doc(GDMModel" in code


def test_infer_from_drive_explorer_no_content_calls() -> None:
    service = InMemoryDriveService(
        children={
            "folder": [
                {"id": "1", "name": "a.json", "mimeType": "application/json"},
            ]
        }
    )
    report = Explorer(service).explore("folder")
    infer_from_collection_inventory(
        report.inventory.resources,
        folder_id="folder",
        model_name="Files",
    )
    assert all("files.list" in c for c in service.call_log)
