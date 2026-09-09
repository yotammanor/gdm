"""Exercise the FastAPI example without Google credentials."""

from __future__ import annotations

import os
from collections.abc import Iterator
from importlib.util import find_spec
from typing import Any

import pytest

from gdm.errors import ConflictError
from gdm.session import Session


@pytest.fixture
def demo_client() -> Iterator[Any]:
    if find_spec("fastapi") is None:
        # Skipping locally is fine; skipping on CI would hide the example.
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("CI must run pytest with --all-groups to install fastapi.")
        pytest.skip("Install the examples group: uv run --all-groups pytest")
    from fastapi.testclient import TestClient
    from fastapi_assets.demo import build_demo_app

    with TestClient(build_demo_app()) as client:
        yield client


def test_lists_seeded_assets(demo_client) -> None:
    response = demo_client.get("/assets")

    assert response.status_code == 200
    payload = response.json()
    assert [asset["name"] for asset in payload] == [
        "launch-brief.png",
        "launch-deck",
        "teaser.pdf",
    ]
    assert payload[0]["mime_type"] == "image/png"
    assert payload[0]["approved"] is True


def test_filters_by_campaign_and_approval(demo_client) -> None:
    by_campaign = demo_client.get("/assets", params={"campaign": "Q1"})
    assert [asset["drive_id"] for asset in by_campaign.json()] == [
        "file-brief",
        "file-deck",
    ]

    unapproved = demo_client.get("/assets", params={"approved": False})
    assert [asset["drive_id"] for asset in unapproved.json()] == [
        "file-deck",
        "file-teaser",
    ]


def test_limit_is_bounded(demo_client) -> None:
    assert len(demo_client.get("/assets", params={"limit": 2}).json()) == 2
    assert demo_client.get("/assets", params={"limit": 0}).status_code == 422


def test_reads_one_asset_by_drive_id(demo_client) -> None:
    response = demo_client.get("/assets/file-deck")

    assert response.status_code == 200
    assert response.json()["name"] == "launch-deck"


def test_unknown_asset_is_404(demo_client) -> None:
    response = demo_client.get("/assets/file-missing")

    assert response.status_code == 404
    assert "file-missing" in response.json()["detail"]


def test_patch_persists_metadata(demo_client) -> None:
    updated = demo_client.patch("/assets/file-deck", json={"approved": True})

    assert updated.status_code == 200
    assert updated.json()["approved"] is True
    assert demo_client.get("/assets/file-deck").json()["approved"] is True


def test_patch_leaves_unset_fields_alone(demo_client) -> None:
    demo_client.patch("/assets/file-brief", json={"campaign": "Q3"})

    asset = demo_client.get("/assets/file-brief").json()
    assert asset["campaign"] == "Q3"
    assert asset["approved"] is True


def test_patch_rejects_drive_owned_fields(demo_client) -> None:
    response = demo_client.patch("/assets/file-brief", json={"name": "renamed.png"})

    assert response.status_code == 422
    assert demo_client.get("/assets/file-brief").json()["name"] == "launch-brief.png"


def test_concurrent_drive_edit_is_409(demo_client, monkeypatch) -> None:
    def raise_conflict(self: Session) -> None:
        raise ConflictError("Record was edited in Drive since it was read.")

    monkeypatch.setattr(Session, "commit", raise_conflict)

    response = demo_client.patch("/assets/file-deck", json={"approved": True})

    assert response.status_code == 409
    assert "edited in Drive" in response.json()["detail"]


def test_content_route_needs_a_live_drive_bind(demo_client) -> None:
    response = demo_client.get("/assets/file-brief/content")

    assert response.status_code == 503
    assert "live Drive bind" in response.json()["detail"]


def test_openapi_documents_the_gdm_model(demo_client) -> None:
    schemas = demo_client.get("/openapi.json").json()["components"]["schemas"]

    assert "campaign" in schemas["CampaignAsset"]["properties"]
    assert "mime_type" in schemas["CampaignAsset"]["properties"]
    assert sorted(schemas["AssetUpdate"]["properties"]) == ["approved", "campaign"]
