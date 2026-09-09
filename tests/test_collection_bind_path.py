"""Collection path hint binding and auto-resolution tests."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.client import GDMClient
from gdm.collection.constants import FOLDER_MIME
from gdm.errors import AmbiguousResourceError, ForeignProjectError, NotFoundError
from gdm.fields import Field
from gdm.model import GDMModel
from gdm.project import Project
from tests.collection_fakes import CollectionMemoryDriveService, seed_mixed_collection
from tests.fakes import FakeSheetsService


class CampaignAsset(
    GDMModel,
    storage="collection",
    collection="Campaigns/Launch",
    mapping="partial",
):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    campaign: str | None = Field(default=None, metadata="campaign")


class UnhintedAsset(GDMModel, storage="collection", mapping="partial"):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")


def _client_setup() -> dict[str, Any]:
    drive_service = CollectionMemoryDriveService()
    root_id = drive_service.files.seed(
        file_id="root-bind",
        name="Root",
        mime_type=FOLDER_MIME,
        parents=[],
    )
    campaigns = drive_service.files.seed(
        name="Campaigns",
        mime_type=FOLDER_MIME,
        parents=[root_id],
    )
    launch = drive_service.files.seed(
        name="Launch",
        mime_type=FOLDER_MIME,
        parents=[campaigns],
    )
    seed_mixed_collection(drive_service.files, launch)
    project = Project(
        project_id=uuid.uuid4(),
        root_folder_id=root_id,
        drive=drive_service,
    )
    project.initialize()
    drive = DriveBackend(drive_service, project_id=str(project.project_id))
    sheets = SheetsBackend(FakeSheetsService([["_gdm_file_id", "_gdm_version"]]))
    backend = RegistryBackend(project=project, drive=drive, sheets=sheets)
    client = GDMClient(
        project=project,
        drive=drive,
        sheets=sheets,
        registry_backend=backend,
        drive_service=drive_service,  # type: ignore[arg-type]
    )
    return {
        "client": client,
        "backend": backend,
        "project": project,
        "launch_id": launch,
        "drive_service": drive_service,
    }


def test_bind_collection_by_path_persists_folder_id() -> None:
    setup = _client_setup()
    entry = setup["client"].bind_collection(CampaignAsset, path="Campaigns/Launch")
    assert entry.resource_id == setup["launch_id"]
    assert entry.extra.get("collection_path") == "Campaigns/Launch"
    reopened = Project.open(
        root_folder_id=setup["project"].root_folder_id,
        drive=setup["drive_service"],
    )
    stored = reopened.get_registered(CampaignAsset.gdm_model_name())
    assert stored.resource_id == setup["launch_id"]


def test_bind_collection_rejects_both_path_and_id() -> None:
    setup = _client_setup()
    with pytest.raises(ValueError, match="not both"):
        setup["client"].bind_collection(
            CampaignAsset,
            folder_id=setup["launch_id"],
            path="Campaigns/Launch",
        )


def test_bind_collection_by_id_wins_over_model_hint() -> None:
    setup = _client_setup()
    other = setup["drive_service"].files.seed(
        name="Other",
        mime_type=FOLDER_MIME,
        parents=[setup["project"].root_folder_id],
    )
    entry = setup["client"].bind_collection(CampaignAsset, folder_id=other)
    assert entry.resource_id == other


def test_client_collection_auto_resolves_unbound_model_hint() -> None:
    setup = _client_setup()
    coll = setup["client"].collection(CampaignAsset)
    assert coll.folder_id == setup["launch_id"]
    entry = setup["project"].get_registered(CampaignAsset.gdm_model_name())
    assert entry.resource_id == setup["launch_id"]


def test_client_collection_without_hint_requires_binding() -> None:
    setup = _client_setup()
    with pytest.raises(NotFoundError):
        setup["client"].collection(UnhintedAsset)


def test_bind_collection_missing_path_raises() -> None:
    setup = _client_setup()
    with pytest.raises(NotFoundError):
        setup["client"].bind_collection(CampaignAsset, path="Campaigns/Missing")


def test_bind_collection_ambiguous_path_raises() -> None:
    setup = _client_setup()
    campaigns = setup["project"].resolve_path("Campaigns")
    setup["drive_service"].files.seed(
        name="Launch",
        mime_type=FOLDER_MIME,
        parents=[campaigns],
    )
    with pytest.raises(AmbiguousResourceError):
        setup["client"].bind_collection(CampaignAsset, path="Campaigns/Launch")


def test_bind_collection_rejects_foreign_project_folder() -> None:
    setup = _client_setup()
    foreign = setup["drive_service"].files.seed(
        name="Foreign",
        mime_type=FOLDER_MIME,
        parents=[setup["project"].root_folder_id],
        appProperties={"gdm_project": str(uuid.uuid4())},
    )
    with pytest.raises(ForeignProjectError):
        setup["client"].bind_collection(CampaignAsset, folder_id=foreign)


def test_auto_resolve_only_runs_once() -> None:
    setup = _client_setup()
    setup["client"].collection(CampaignAsset)
    entry = setup["project"].get_registered(CampaignAsset.gdm_model_name())
    original_id = entry.resource_id
    setup["drive_service"].files.seed(
        name="Launch",
        mime_type=FOLDER_MIME,
        parents=[setup["project"].resolve_path("Campaigns")],
    )
    coll = setup["client"].collection(CampaignAsset)
    assert coll.folder_id == original_id
