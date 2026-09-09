"""Collection candidate analysis from Drive inventories."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from gdm.explore_types import DriveResource, ResourceKind
from gdm.tooling.mime import MimeCategory, categorize_mime


@dataclass(frozen=True, slots=True)
class MimeDistribution:
    """Counts per MIME category."""

    counts: Mapping[MimeCategory, int]

    def to_dict(self) -> dict[str, int]:
        return {cat.value: count for cat, count in sorted(self.counts.items())}


@dataclass(frozen=True, slots=True)
class CollectionBoundary:
    """One folder boundary in the inventory."""

    folder_id: str
    name: str
    path: tuple[str, ...]
    direct_child_count: int
    nested_descendant_count: int
    has_gdm_metadata: bool
    gdm_project: str | None = None


@dataclass(frozen=True, slots=True)
class CollectionCandidate:
    """Folder that likely maps to a ``storage='collection'`` model."""

    folder_id: str
    name: str
    path: tuple[str, ...]
    direct_child_count: int
    nested_descendant_count: int
    mime_distribution: MimeDistribution
    duplicate_name_groups: int
    shortcut_count: int
    has_gdm_metadata: bool
    gdm_project: str | None = None
    gdm_model: str | None = None
    likely_drive_fields: tuple[str, ...] = field(
        default_factory=lambda: ("id", "name", "mimeType")
    )


def analyze_mime_distribution(
    resources: Sequence[DriveResource],
) -> MimeDistribution:
    """Count resources by MIME category."""
    counts: dict[MimeCategory, int] = defaultdict(int)
    for resource in resources:
        counts[categorize_mime(resource.mime_type)] += 1
    return MimeDistribution(counts=dict(counts))


def _descendants_by_folder(
    resources: Sequence[DriveResource],
) -> dict[str, list[DriveResource]]:
    by_parent: dict[str | None, list[DriveResource]] = defaultdict(list)
    for resource in resources:
        by_parent[resource.parent_id].append(resource)
    memo: dict[str, list[DriveResource]] = {}

    def collect(folder_id: str) -> list[DriveResource]:
        if folder_id in memo:
            return memo[folder_id]
        descendants: list[DriveResource] = []
        for child in by_parent.get(folder_id, []):
            descendants.append(child)
            if child.kind is ResourceKind.FOLDER:
                descendants.extend(collect(child.id))
        memo[folder_id] = descendants
        return descendants

    return {
        folder.id: collect(folder.id)
        for folder in resources
        if folder.kind is ResourceKind.FOLDER
    }


def _duplicate_groups_under(resources: Sequence[DriveResource], parent_id: str) -> int:
    names: dict[str, int] = defaultdict(int)
    for resource in resources:
        if resource.parent_id == parent_id:
            names[resource.name] += 1
    return sum(1 for count in names.values() if count > 1)


def analyze_collection_candidates(
    resources: Sequence[DriveResource],
    *,
    root_id: str,
) -> tuple[CollectionCandidate, ...]:
    """Identify folder collection candidates and per-folder boundaries."""
    folders = [r for r in resources if r.kind is ResourceKind.FOLDER]
    descendants = _descendants_by_folder(resources)
    candidates: list[CollectionCandidate] = []

    for folder in folders:
        direct = [r for r in resources if r.parent_id == folder.id]
        nested = descendants.get(folder.id, [])
        nested_only = [r for r in nested if r.parent_id != folder.id]
        mime_dist = analyze_mime_distribution(direct)
        gdm_projects = {r.gdm_project for r in direct if r.gdm_project}
        gdm_models = {r.gdm_model for r in direct if r.gdm_model}
        has_gdm = folder.has_gdm_metadata or any(r.has_gdm_metadata for r in direct)
        candidates.append(
            CollectionCandidate(
                folder_id=folder.id,
                name=folder.name,
                path=folder.path,
                direct_child_count=len(direct),
                nested_descendant_count=len(nested_only),
                mime_distribution=mime_dist,
                duplicate_name_groups=_duplicate_groups_under(resources, folder.id),
                shortcut_count=sum(
                    1
                    for r in direct
                    if categorize_mime(r.mime_type) is MimeCategory.SHORTCUT
                ),
                has_gdm_metadata=has_gdm,
                gdm_project=next(iter(gdm_projects), None),
                gdm_model=next(iter(gdm_models), None),
            )
        )

    candidates.sort(
        key=lambda c: (
            c.folder_id != root_id,
            -c.direct_child_count,
            c.path,
        )
    )
    return tuple(candidates)


def collection_boundaries(
    resources: Sequence[DriveResource],
) -> tuple[CollectionBoundary, ...]:
    """Return every folder boundary with direct vs nested counts."""
    descendants = _descendants_by_folder(resources)
    boundaries: list[CollectionBoundary] = []
    for folder in resources:
        if folder.kind is not ResourceKind.FOLDER:
            continue
        direct = sum(1 for r in resources if r.parent_id == folder.id)
        nested = len(descendants.get(folder.id, [])) - direct
        boundaries.append(
            CollectionBoundary(
                folder_id=folder.id,
                name=folder.name,
                path=folder.path,
                direct_child_count=direct,
                nested_descendant_count=max(nested, 0),
                has_gdm_metadata=folder.has_gdm_metadata,
                gdm_project=folder.gdm_project,
            )
        )
    return tuple(boundaries)
