"""MIME categorization for collection exploration and inference."""

from __future__ import annotations

from enum import StrEnum

from gdm.collection.constants import (
    DOC_MIME,
    FOLDER_MIME,
    SHEET_MIME,
    SHORTCUT_MIME,
    SLIDE_MIME,
)


class MimeCategory(StrEnum):
    """High-level MIME families tracked by the explorer."""

    FOLDER = "folder"
    SHORTCUT = "shortcut"
    DOC = "doc"
    SLIDE = "slide"
    SHEET = "sheet"
    IMAGE = "image"
    PDF = "pdf"
    BINARY = "binary"
    OTHER = "other"


def categorize_mime(mime_type: str) -> MimeCategory:
    """Map a Drive MIME type to a collection-relevant category."""
    if mime_type == FOLDER_MIME:
        return MimeCategory.FOLDER
    if mime_type == SHORTCUT_MIME:
        return MimeCategory.SHORTCUT
    if mime_type == DOC_MIME:
        return MimeCategory.DOC
    if mime_type == SLIDE_MIME:
        return MimeCategory.SLIDE
    if mime_type == SHEET_MIME:
        return MimeCategory.SHEET
    if mime_type == "application/pdf":
        return MimeCategory.PDF
    if mime_type.startswith("image/"):
        return MimeCategory.IMAGE
    if mime_type.startswith("application/vnd.google-apps."):
        return MimeCategory.OTHER
    if mime_type.startswith("text/") or mime_type == "application/json":
        return MimeCategory.OTHER
    return MimeCategory.BINARY


def mime_selector_for_category(category: MimeCategory) -> str | None:
    """Return a model ``mime=`` pattern when one category dominates."""
    mapping = {
        MimeCategory.DOC: "application/vnd.google-apps.document",
        MimeCategory.SLIDE: "application/vnd.google-apps.presentation",
        MimeCategory.SHEET: "application/vnd.google-apps.spreadsheet",
        MimeCategory.IMAGE: "image/*",
        MimeCategory.PDF: "application/pdf",
    }
    return mapping.get(category)
