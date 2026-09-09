"""Run the app on seeded in-memory data instead of Google Drive.

The metadata routes behave exactly as they do against Drive, which is what makes
the app testable without credentials. Native content is the one thing an
in-memory backend cannot stand in for, so ``/content`` reports 503 here.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, NoReturn

from fastapi import FastAPI, HTTPException

from fastapi_assets.app import create_app, get_assets, get_session
from fastapi_assets.models import CampaignAsset
from gdm import Session
from gdm.backends.memory import MemoryBackend
from gdm.testing import session_context

DEMO_ASSETS: tuple[dict[str, Any], ...] = (
    {
        "drive_id": "file-brief",
        "name": "launch-brief.png",
        "mime_type": "image/png",
        "campaign": "Q1",
        "approved": True,
    },
    {
        "drive_id": "file-deck",
        "name": "launch-deck",
        "mime_type": "application/vnd.google-apps.presentation",
        "campaign": "Q1",
        "approved": False,
    },
    {
        "drive_id": "file-teaser",
        "name": "teaser.pdf",
        "mime_type": "application/pdf",
        "campaign": "Q2",
        "approved": False,
    },
)


def seeded_backend() -> MemoryBackend:
    """Return a memory backend holding the demo assets."""
    backend = MemoryBackend()
    for values in DEMO_ASSETS:
        backend.seed_record(
            CampaignAsset,
            dict(values),
            record_key=str(values["drive_id"]),
        )
    return backend


def build_demo_app(backend: MemoryBackend | None = None) -> FastAPI:
    """Build the app with Drive swapped for ``backend``."""
    active = backend if backend is not None else seeded_backend()

    def session_override() -> Iterator[Session]:
        with session_context(active) as session:
            yield session

    def assets_override() -> NoReturn:
        raise HTTPException(
            status_code=503,
            detail="File content needs a live Drive bind; run app.py instead.",
        )

    demo = create_app()
    demo.dependency_overrides[get_session] = session_override
    demo.dependency_overrides[get_assets] = assets_override
    return demo


app = build_demo_app()
