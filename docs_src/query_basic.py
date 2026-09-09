"""Query collection metadata through a session (no body downloads)."""

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
            rows = session.exec(
                select(CampaignAsset)
                .where(CampaignAsset.approved == True)  # noqa: E712
                .limit(50)
            ).all()
            print(f"Approved: {len(rows)}")
        return

    client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
    with client.session(scan_limit=500) as session:
        rows = session.exec(
            select(CampaignAsset)
            .where(CampaignAsset.campaign == "Q1")
            .order_by(CampaignAsset.name)
            .limit(50)
        ).all()
        print(f"Q1 assets: {len(rows)}")


if __name__ == "__main__":
    main()
