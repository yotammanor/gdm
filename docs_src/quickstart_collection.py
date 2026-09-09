"""Quickstart: open a project and bind a Drive folder collection."""

from __future__ import annotations

import os

from models_campaign_asset import CampaignAsset

from gdm import GDM, select
from gdm.backends.memory import MemoryBackend
from gdm.testing import session_context


def _seed_demo_assets(backend: MemoryBackend) -> None:
    backend.seed_record(
        CampaignAsset,
        {
            "drive_id": "file-brief",
            "name": "launch-brief.png",
            "mime_type": "image/png",
            "campaign": "Q1",
            "approved": True,
        },
        record_key="file-brief",
    )
    backend.seed_record(
        CampaignAsset,
        {
            "drive_id": "file-deck",
            "name": "launch-deck",
            "mime_type": "application/vnd.google-apps.presentation",
            "campaign": "Q1",
            "approved": False,
        },
        record_key="file-deck",
    )


def main() -> None:
    if os.environ.get("GDM_USE_FAKE") == "1":
        backend = MemoryBackend()
        _seed_demo_assets(backend)
        with session_context(backend) as session:
            rows = session.exec(select(CampaignAsset).limit(10)).all()
            for asset in rows:
                print(asset.drive_id, asset.name, asset.mime_type, asset.campaign)
            approved = session.exec(
                select(CampaignAsset).where(CampaignAsset.approved == True)  # noqa: E712
            ).all()
            print(f"Approved assets: {len(approved)}")
        print(
            "Offline mode: seeded memory backend for metadata queries. "
            "client.collection() listing and content handles require a live Drive bind."
        )
        return

    client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
    client.bind_collection(
        CampaignAsset,
        folder_id="YOUR_CAMPAIGNS_FOLDER_ID",
    )

    assets = client.collection(CampaignAsset)
    for asset in assets.list():
        print(asset.drive_id, asset.name, asset.mime_type, asset.campaign)

    images = assets.where_mime("image/*")
    brief = images.one_by_name("launch-brief.png")
    print(brief.content.download()[:80])

    with client.session() as session:
        approved = session.exec(
            select(CampaignAsset).where(CampaignAsset.approved == True)  # noqa: E712
        ).all()
        print(f"Approved assets: {len(approved)}")


if __name__ == "__main__":
    main()
