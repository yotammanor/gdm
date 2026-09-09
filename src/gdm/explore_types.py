"""Shared Drive exploration types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ResourceKind(StrEnum):
    """Drive resource kinds tracked by the explorer."""

    FOLDER = "folder"
    FILE = "file"
    SPREADSHEET = "spreadsheet"
    WORKSHEET = "worksheet"
    SHORTCUT = "shortcut"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DriveResource:
    """One Drive resource in an inventory."""

    id: str
    name: str
    mime_type: str
    kind: ResourceKind
    parent_id: str | None
    size: int | None = None
    shared_drive_id: str | None = None
    path: tuple[str, ...] = field(default_factory=tuple)
    shortcut_target_id: str | None = None
    shortcut_target_mime: str | None = None
    app_properties: Mapping[str, str] = field(default_factory=dict)
    properties: Mapping[str, str] = field(default_factory=dict)
    has_gdm_metadata: bool = False
    gdm_project: str | None = None
    gdm_model: str | None = None
    gdm_schema: str | None = None
    depth: int = 0
    is_direct_child_of_root: bool = False


@dataclass(frozen=True, slots=True)
class DuplicateNameGroup:
    """Group of resources sharing the same name under one parent."""

    parent_id: str | None
    name: str
    resource_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TypeCandidate:
    """Candidate model/storage binding inferred from MIME and shape."""

    mime_type: str
    count: int
    total_size: int
    kind: ResourceKind
    category: Any
    sample_names: tuple[str, ...]
