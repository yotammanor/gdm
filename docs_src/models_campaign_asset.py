"""Collection model for a mixed Drive folder (Docs, Slides, images, PDFs, …)."""

from gdm import DriveObjectMixin, Field, GDMModel


class CampaignAsset(
    GDMModel,
    DriveObjectMixin,
    storage="collection",
    collection="Campaigns/Launch",
    mapping="partial",
):
    campaign: str | None = Field(default=None, metadata="campaign")
    approved: bool = Field(default=False, metadata="approved")
