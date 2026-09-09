"""A JSON API over one Drive folder, using a GDM session per request.

Routes are plain ``def``, not ``async def``: GDM is synchronous, so FastAPI runs
them in its threadpool instead of blocking the event loop on Google API calls.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict

from fastapi_assets.models import CampaignAsset
from gdm import (
    GDM,
    ConflictError,
    DriveCollection,
    GDMClient,
    NotFoundError,
    Session,
    UnsupportedOperationError,
    select,
)


class AssetUpdate(BaseModel):
    """Client-writable metadata. Drive-owned fields stay read-only."""

    model_config = ConfigDict(extra="forbid")

    campaign: str | None = None
    approved: bool | None = None


@lru_cache(maxsize=1)
def get_client() -> GDMClient:
    """Open the Drive-backed project once per process."""
    client = GDM.open(root_folder_id=os.environ["GDM_ROOT_FOLDER_ID"])
    folder_id = os.environ.get("GDM_ASSETS_FOLDER_ID")
    if folder_id:
        client.bind_collection(CampaignAsset, folder_id=folder_id)
    return client


def get_session() -> Iterator[Session]:
    """Yield one session per request."""
    with get_client().session() as session:
        yield session


def get_assets() -> DriveCollection[CampaignAsset]:
    """Return the collection bound to the campaign folder."""
    return get_client().collection(CampaignAsset)


SessionDep = Annotated[Session, Depends(get_session)]
AssetsDep = Annotated[DriveCollection[CampaignAsset], Depends(get_assets)]

router = APIRouter(prefix="/assets", tags=["assets"])


def _load_asset(session: Session, drive_id: str) -> CampaignAsset:
    asset = session.get(CampaignAsset, drive_id)
    if not isinstance(asset, CampaignAsset):
        raise HTTPException(
            status_code=404,
            detail=f"No asset {drive_id!r} in this collection.",
        )
    return asset


@router.get("")
def list_assets(
    session: SessionDep,
    campaign: str | None = None,
    approved: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CampaignAsset]:
    """List assets. Metadata only — no file body is downloaded."""
    statement = select(CampaignAsset).limit(limit)
    if campaign is not None:
        statement = statement.where(CampaignAsset.campaign == campaign)
    if approved is not None:
        statement = statement.where(CampaignAsset.approved == approved)
    return session.exec(statement).all()


@router.get("/{drive_id}")
def read_asset(drive_id: str, session: SessionDep) -> CampaignAsset:
    """Read one asset by its Drive file ID."""
    return _load_asset(session, drive_id)


@router.patch("/{drive_id}")
def update_asset(
    drive_id: str,
    payload: AssetUpdate,
    session: SessionDep,
) -> CampaignAsset:
    """Update application metadata without clobbering a concurrent Drive edit."""
    asset = _load_asset(session, drive_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(asset, field, value)
    session.add(asset)
    try:
        session.commit()
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return asset


@router.get("/{drive_id}/content")
def download_asset(drive_id: str, assets: AssetsDep) -> Response:
    """Serve the file body. This is the only route that reads content."""
    try:
        asset = assets.get(drive_id)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    content = asset.content
    try:
        return Response(content=content.download(), media_type=content.mime_type)
    except UnsupportedOperationError:
        # Docs, Slides, and Sheets have no binary body; export a portable view.
        pdf = content.export("application/pdf")
        return Response(content=pdf, media_type="application/pdf")


def create_app() -> FastAPI:
    """Build the app. Tests and the demo swap dependencies on the instance."""
    app = FastAPI(
        title="Campaign assets",
        description=__doc__,
        version="0.1.0",
    )
    app.include_router(router)
    return app


app = create_app()
