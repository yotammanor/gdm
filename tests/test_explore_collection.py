"""Collection-aware explorer tests."""

from __future__ import annotations

from gdm.explore import Explorer, InMemoryDriveService, inventory_to_dict
from gdm.tooling.mime import MimeCategory


def test_explore_reports_collection_candidates_and_mime_distribution() -> None:
    service = InMemoryDriveService(
        children={
            "root": [
                {
                    "id": "folder",
                    "name": "Campaigns",
                    "mimeType": "application/vnd.google-apps.folder",
                },
                {
                    "id": "gdm",
                    "name": ".gdm",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            ],
            "folder": [
                {
                    "id": "img1",
                    "name": "hero.png",
                    "mimeType": "image/png",
                    "size": "100",
                },
                {
                    "id": "doc1",
                    "name": "brief",
                    "mimeType": "application/vnd.google-apps.document",
                },
                {
                    "id": "sc1",
                    "name": "link",
                    "mimeType": "application/vnd.google-apps.shortcut",
                    "shortcutDetails": {
                        "targetId": "doc1",
                        "targetMimeType": "application/vnd.google-apps.document",
                    },
                },
                {
                    "id": "dup-a",
                    "name": "dup",
                    "mimeType": "text/plain",
                },
                {
                    "id": "dup-b",
                    "name": "dup",
                    "mimeType": "text/plain",
                },
            ],
        }
    )
    report = Explorer(service).explore("root")
    inv = report.inventory
    assert inv.zero_content_reads is True
    assert inv.direct_count >= 1
    assert inv.mime_distribution.get(MimeCategory.IMAGE, 0) >= 1
    assert len(inv.collection_candidates) >= 1
    assert any(r.shortcut_target_id == "doc1" for r in inv.resources)
    payload = inventory_to_dict(report)
    assert payload["zero_content_reads"] is True
    assert "collection_candidates" in payload


def test_explore_only_uses_files_list() -> None:
    service = InMemoryDriveService(children={"root": []})
    explorer = Explorer(service)
    explorer.explore("root")
    assert all(call.startswith("files.list:") for call in service.call_log)
    forbidden = ("media", "export", "documents", "presentations", "spreadsheets")
    joined = " ".join(service.call_log)
    assert not any(token in joined for token in forbidden)


def test_shared_drive_pagination() -> None:
    children = [
        {"id": str(i), "name": f"f{i}", "mimeType": "text/plain", "driveId": "sd1"}
        for i in range(5)
    ]
    service = InMemoryDriveService(children={"root": children})
    report = Explorer(service, page_size=2).explore("root")
    assert report.inventory.total_count == 5
    assert len(service.call_log) > 1
