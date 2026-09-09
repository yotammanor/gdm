"""Opt-in JSON codec for greenfield file collections (not native Google Docs)."""

from gdm import Field, GDMModel


class ReleaseNote(
    GDMModel,
    storage="file",
    codec="json",
    collection="Release Notes",
    mapping="partial",
):
    id: str | None = Field(default=None, primary_key=True)
    version: str = Field(index=True)
    body: str
    published: bool = False
