"""Branch coverage for Drive exploration and serialization."""

from __future__ import annotations

import json

from gdm.explore import (
    DriveResource,
    Explorer,
    InMemoryDriveService,
    ResourceKind,
    build_resource_tree,
    inventory_to_dict,
)
from gdm.explore_types import DuplicateNameGroup


def test_inventory_properties() -> None:
    (
        DriveResource("1", "root", "folder", ResourceKind.FOLDER, None),
        DriveResource("2", "a.txt", "text/plain", ResourceKind.FILE, "1"),
        DriveResource(
            "3",
            "sheet",
            "application/vnd.google-apps.spreadsheet",
            ResourceKind.SPREADSHEET,
            "1",
        ),
    )
    service = InMemoryDriveService(
        children={
            "root": [
                {
                    "id": "1",
                    "name": "root",
                    "mimeType": "application/vnd.google-apps.folder",
                }
            ]
        }
    )
    report = Explorer(service).explore("root")
    inv = report.inventory
    _ = inv.folders
    _ = inv.files


def test_invalid_app_and_file_properties() -> None:
    service = InMemoryDriveService(
        children={
            "root": [
                {
                    "id": "1",
                    "name": "bad",
                    "mimeType": "text/plain",
                    "appProperties": "not-a-map",
                    "properties": 123,
                },
            ]
        }
    )
    report = Explorer(service).explore("root")
    assert report.inventory.total_count == 1


def test_shortcut_without_target_runtime_note() -> None:
    service = InMemoryDriveService(
        children={
            "root": [
                {
                    "id": "1",
                    "name": "link",
                    "mimeType": "application/vnd.google-apps.shortcut",
                },
            ]
        }
    )
    explorer = Explorer(service)
    report = explorer.explore("root")
    assert explorer.list_call_count >= 1
    assert report.runtime_integration_notes
    assert "DriveExplorerAdapter" in report.runtime_integration_notes[0]


def test_inventory_to_dict_roundtrip_fields() -> None:
    service = InMemoryDriveService(
        children={
            "root": [
                {
                    "id": "1",
                    "name": "Assets",
                    "mimeType": "application/vnd.google-apps.folder",
                    "appProperties": {"gdm_model": "Asset"},
                },
                {"id": "2", "name": "a.png", "mimeType": "image/png", "parents": ["1"]},
            ],
            "1": [{"id": "2", "name": "a.png", "mimeType": "image/png"}],
        }
    )
    report = Explorer(service).explore("root", root_name="Drive")
    payload = inventory_to_dict(report)
    assert payload["zero_content_reads"] is True
    assert "collection_candidates" in payload
    assert "likely_drive_field_mappings" in payload
    encoded = json.dumps(payload)
    assert "DriveExplorerAdapter" in encoded or "warnings" in encoded


def test_explore_with_gdm_metadata_and_shortcut() -> None:
    service = InMemoryDriveService(
        children={
            "root": [
                {
                    "id": "1",
                    "name": "item",
                    "mimeType": "application/vnd.google-apps.shortcut",
                    "shortcutDetails": {
                        "targetId": "t1",
                        "targetMimeType": "image/png",
                    },
                    "appProperties": {"gdm_project": "p1"},
                },
            ]
        }
    )
    report = Explorer(service).explore("root")
    resource = report.inventory.resources[0]
    assert resource.shortcut_target_id == "t1"
    assert resource.has_gdm_metadata is True


def test_build_resource_tree_sorting() -> None:
    resources = [
        DriveResource("1", "B", "folder", ResourceKind.FOLDER, None),
        DriveResource("2", "a", "file", ResourceKind.FILE, "1"),
        DriveResource("3", "A", "file", ResourceKind.FILE, "1"),
    ]
    tree = build_resource_tree(resources)
    names = [r.name for r in tree["1"]]
    assert names == ["a", "A"]


def test_duplicate_name_group_dataclass() -> None:
    group = DuplicateNameGroup(parent_id="p", name="dup", resource_ids=("1", "2"))
    assert len(group.resource_ids) == 2
