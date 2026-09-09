"""Unit tests for collection inventory helpers."""

from __future__ import annotations

import pytest

from gdm.collection.inventory import (
    DriveInventory,
    inventory_item_from_file,
    mime_matches,
)
from gdm.errors import AmbiguousResourceError, NotFoundError
from tests.collection_fakes import CollectionMemoryDriveService


def test_mime_matches_patterns() -> None:
    assert mime_matches("image/png", "image/*")
    assert mime_matches("application/pdf", "application/pdf")
    assert not mime_matches("text/plain", "image/*")


def test_inventory_list_and_find() -> None:
    service = CollectionMemoryDriveService()
    folder = service.files.seed(
        name="Folder", mime_type="application/vnd.google-apps.folder", parents=[]
    )
    service.files.seed(
        name="a", mime_type="application/vnd.google-apps.document", parents=[folder]
    )
    service.files.seed(
        name="a", mime_type="application/vnd.google-apps.document", parents=[folder]
    )
    inv = DriveInventory(service.files, folder_id=folder)
    assert len(inv.find_by_name("a")) == 2
    with pytest.raises(AmbiguousResourceError):
        inv.one_by_name("a")
    with pytest.raises(NotFoundError):
        inv.get("missing")


def test_inventory_item_from_shortcut() -> None:
    item = inventory_item_from_file(
        {
            "id": "s1",
            "name": "shortcut",
            "mimeType": "application/vnd.google-apps.shortcut",
            "shortcutDetails": {
                "targetId": "t1",
                "targetMimeType": "application/vnd.google-apps.document",
            },
            "parents": ["p1"],
        }
    )
    assert item.is_shortcut
    assert item.shortcut_target_id == "t1"
