"""Model for a mixed Drive folder of campaign assets."""

from __future__ import annotations

from gdm import DriveObject, Field


class CampaignAsset(DriveObject, collection="Campaigns/Launch"):
    """One native Drive file — Doc, deck, image, or PDF — in the campaign folder.

    Drive owns identity and file metadata (inherited from ``DriveObject``); the
    two fields below live in the collection metadata table that GDM manages.
    """

    campaign: str | None = Field(default=None, metadata="campaign")
    approved: bool = Field(default=False, metadata="approved")
