# ruff: noqa: N803, N802
"""Tests for gdm.sync."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gdm.sync import WEBHOOK_NOTICE, ChangeRecord, ChangesPoller, SyncCheckpoint


class FakeChanges:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def getStartPageToken(
        self,
        *,
        supportsAllDrives: bool = True,
        driveId: str | None = None,
    ) -> dict:
        del supportsAllDrives, driveId
        return {"startPageToken": "start-1"}

    def list(
        self,
        *,
        pageToken: str,
        pageSize: int | None = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
        includeItemsFromAllDrives: bool = True,
        driveId: str | None = None,
        restrictToMyDrive: bool | None = None,
        includeRemoved: bool = True,
    ) -> dict:
        del pageSize, fields, supportsAllDrives, includeItemsFromAllDrives
        del driveId, restrictToMyDrive, includeRemoved
        self.calls.append(pageToken)
        if pageToken == "start-1":
            return {
                "nextPageToken": "page-2",
                "changes": [
                    {
                        "changeId": "1",
                        "fileId": "f-1",
                        "removed": False,
                        "file": {"id": "f-1", "name": "a"},
                    }
                ],
            }
        return {
            "newStartPageToken": "start-2",
            "changes": [
                {
                    "changeId": "2",
                    "fileId": "f-2",
                    "removed": True,
                }
            ],
        }


class FakeDriveService:
    def __init__(self) -> None:
        self.changes = FakeChanges()


@pytest.fixture
def poller() -> ChangesPoller:
    return ChangesPoller(FakeDriveService(), shared_drive_id="sd-1")


def test_webhook_notice() -> None:
    assert "optional accelerators" in WEBHOOK_NOTICE
    assert ChangesPoller.webhook_notice() == WEBHOOK_NOTICE


def test_fetch_start_token(poller: ChangesPoller) -> None:
    assert poller.fetch_start_token() == "start-1"


def test_initial_checkpoint(poller: ChangesPoller) -> None:
    cp = poller.initial_checkpoint()
    assert cp.start_page_token == "start-1"
    assert cp.shared_drive
    assert cp.drive_id == "sd-1"


def test_poll_paginates_and_updates_checkpoint(poller: ChangesPoller) -> None:
    checkpoint = SyncCheckpoint(
        start_page_token="start-1", shared_drive=True, drive_id="sd-1"
    )
    page, updated = poller.poll(checkpoint)
    assert len(page.changes) == 2
    assert isinstance(page.changes[0], ChangeRecord)
    assert page.changes[1].removed
    assert updated.start_page_token == "start-2"
    assert updated.last_change_id == "2"
    assert poller.service.changes.calls == ["start-1", "page-2"]


def test_checkpoint_round_trip() -> None:
    cp = SyncCheckpoint(
        start_page_token="t", drive_id="d", shared_drive=True, last_change_id="9"
    )
    restored = SyncCheckpoint.from_dict(cp.to_dict())
    assert restored == cp


def test_malformed_start_token() -> None:
    svc = FakeDriveService()
    svc.changes.getStartPageToken = MagicMock(return_value={})
    poller = ChangesPoller(svc)
    with pytest.raises(Exception, match="startPageToken"):
        poller.fetch_start_token()
