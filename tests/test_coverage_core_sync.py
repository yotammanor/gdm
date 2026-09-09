# ruff: noqa: N803, N802
"""Branch coverage for gdm.sync."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from gdm.errors import GDMError, NotFoundError, PermissionDeniedError
from gdm.sync import ChangesPoller, ChangeType, SyncCheckpoint


class FakeChanges:
    def __init__(self, responses: dict[str, dict] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[str] = []

    def getStartPageToken(
        self,
        *,
        supportsAllDrives: bool = True,
        driveId: str | None = None,
    ) -> dict:
        del supportsAllDrives, driveId
        return self.responses.get("start", {"startPageToken": "token-1"})

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
        return self.responses.get(pageToken, {"changes": []})


class FakeService:
    def __init__(self, changes: FakeChanges) -> None:
        self.changes = changes


def test_sync_checkpoint_to_dict_minimal() -> None:
    cp = SyncCheckpoint(start_page_token="t")
    payload = cp.to_dict()
    assert payload == {"start_page_token": "t", "shared_drive": False}
    restored = SyncCheckpoint.from_dict(payload)
    assert restored.last_change_id is None
    assert restored.drive_id is None


def test_change_type_enum_values() -> None:
    assert ChangeType.FILE.value == "file"
    assert ChangeType.REMOVED.value == "removed"


def test_fetch_start_token_http_error_normalized() -> None:
    changes = FakeChanges()
    service = FakeService(changes)
    poller = ChangesPoller(service, shared_drive_id="drive-1")

    def _boom(**_kwargs: object) -> None:
        resp = MagicMock(status=404)
        raise HttpError(resp, b"missing")

    changes.getStartPageToken = _boom  # type: ignore[method-assign]
    with pytest.raises(NotFoundError):
        poller.fetch_start_token()


def test_fetch_start_token_missing_token_raises_gdm_error() -> None:
    service = FakeService(FakeChanges(responses={"start": {}}))
    poller = ChangesPoller(service)
    with pytest.raises(GDMError, match="startPageToken"):
        poller.fetch_start_token()


def test_poll_without_changes_preserves_last_change_id() -> None:
    changes = FakeChanges(
        responses={
            "start": {"changes": []},
        },
    )
    service = FakeService(changes)
    poller = ChangesPoller(service)
    checkpoint = SyncCheckpoint(start_page_token="start", last_change_id="prev")
    page, updated = poller.poll(checkpoint)
    assert page.changes == []
    assert updated.last_change_id == "prev"


def test_list_changes_http_error_normalized() -> None:
    changes = FakeChanges()

    def _list(**_kwargs: object) -> None:
        resp = MagicMock(status=403)
        raise HttpError(resp, b"denied")

    changes.list = _list  # type: ignore[method-assign]
    poller = ChangesPoller(FakeService(changes))
    with pytest.raises(PermissionDeniedError, match="denied"):
        poller._list_changes("token")


def test_parse_change_defaults() -> None:
    record = ChangesPoller._parse_change({})
    assert record.change_id == ""
    assert record.removed is False


def test_poll_uses_new_start_page_token_when_present() -> None:
    changes = FakeChanges(
        responses={
            "tok": {
                "newStartPageToken": "new-tok",
                "changes": [{"changeId": "9", "removed": False}],
            },
        },
    )
    poller = ChangesPoller(FakeService(changes))
    checkpoint = SyncCheckpoint(start_page_token="tok")
    _, updated = poller.poll(checkpoint)
    assert updated.start_page_token == "new-tok"
    assert updated.last_change_id == "9"
