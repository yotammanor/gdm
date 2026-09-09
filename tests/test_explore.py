"""Tests for Drive exploration."""

from __future__ import annotations

from gdm.explore import (
    Explorer,
    InMemoryDriveService,
    ResourceKind,
    build_resource_tree,
)


def test_explore_detects_duplicate_names() -> None:
    service = InMemoryDriveService(
        children={
            "root": [
                {
                    "id": "1",
                    "name": "dup",
                    "mimeType": "application/vnd.google-apps.document",
                },
                {
                    "id": "2",
                    "name": "dup",
                    "mimeType": "application/vnd.google-apps.document",
                },
                {
                    "id": "3",
                    "name": "folder",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            ],
            "3": [
                {
                    "id": "4",
                    "name": "nested",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "size": "100",
                },
            ],
        }
    )
    report = Explorer(service, page_size=2).explore("root")
    assert report.inventory.total_count == 4
    assert len(report.inventory.duplicates) == 1
    assert report.inventory.duplicates[0].name == "dup"
    assert any(
        c.kind is ResourceKind.SPREADSHEET for c in report.inventory.type_candidates
    )


def test_pagination_and_truncation() -> None:
    children = [
        {"id": str(i), "name": f"f{i}", "mimeType": "text/plain", "size": "1"}
        for i in range(5)
    ]
    service = InMemoryDriveService(children={"root": children})
    report = Explorer(service, page_size=2, max_resources=3).explore("root")
    assert report.inventory.truncated is True
    assert report.inventory.total_count == 3


def test_build_resource_tree() -> None:
    from gdm.explore import DriveResource

    resources = [
        DriveResource("1", "root", "folder", ResourceKind.FOLDER, None),
        DriveResource("2", "a", "file", ResourceKind.FILE, "1"),
    ]
    tree = build_resource_tree(resources)
    assert len(tree[None]) == 1
    assert tree["1"][0].name == "a"


def test_explore_is_read_only() -> None:
    service = InMemoryDriveService(children={"root": []})
    before = dict(service.children)
    Explorer(service).explore("root")
    assert service.children == before
