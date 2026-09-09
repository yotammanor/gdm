"""Read-only Drive exploration with injectable service protocols."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from gdm.collection.constants import FOLDER_MIME, SHORTCUT_MIME
from gdm.explore_types import (
    DriveResource,
    DuplicateNameGroup,
    ResourceKind,
    TypeCandidate,
)
from gdm.tooling.collection_probe import (
    CollectionBoundary,
    CollectionCandidate,
    analyze_collection_candidates,
    analyze_mime_distribution,
    collection_boundaries,
)
from gdm.tooling.mime import MimeCategory, categorize_mime

ZERO_CONTENT_READ_GUARANTEE = (
    "Explorer uses Drive files.list metadata only; no media, export, Docs, "
    "Slides, or Sheets body reads are performed."
)

LIKELY_DRIVE_FIELD_MAPPINGS: tuple[str, ...] = (
    "id",
    "name",
    "mimeType",
    "description",
    "starred",
    "createdTime",
    "modifiedTime",
    "webViewLink",
    "size",
    "md5Checksum",
    "capabilities",
    "shortcutDetails",
    "resourceKey",
)


@dataclass
class ExploreInventory:
    """Aggregated exploration results."""

    root_id: str
    resources: tuple[DriveResource, ...]
    duplicates: tuple[DuplicateNameGroup, ...] = field(default_factory=tuple)
    type_candidates: tuple[TypeCandidate, ...] = field(default_factory=tuple)
    collection_candidates: tuple[CollectionCandidate, ...] = field(
        default_factory=tuple
    )
    collection_boundaries: tuple[CollectionBoundary, ...] = field(default_factory=tuple)
    mime_distribution: Mapping[MimeCategory, int] = field(default_factory=dict)
    direct_count: int = 0
    nested_count: int = 0
    total_count: int = 0
    total_size: int = 0
    truncated: bool = False
    zero_content_reads: bool = True
    content_read_guarantee: str = ZERO_CONTENT_READ_GUARANTEE

    @property
    def folders(self) -> tuple[DriveResource, ...]:
        return tuple(r for r in self.resources if r.kind is ResourceKind.FOLDER)

    @property
    def files(self) -> tuple[DriveResource, ...]:
        return tuple(
            r
            for r in self.resources
            if r.kind not in {ResourceKind.FOLDER, ResourceKind.WORKSHEET}
        )


@dataclass(frozen=True, slots=True)
class ExploreReport:
    """Human-oriented exploration summary."""

    inventory: ExploreInventory
    warnings: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    runtime_integration_notes: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class DriveListPage(Protocol):
    """One page of Drive list results."""

    @property
    def items(self) -> Sequence[Mapping[str, Any]]: ...

    @property
    def next_page_token(self) -> str | None: ...


@runtime_checkable
class DriveExplorerService(Protocol):
    """Injectable Drive listing service."""

    def list_children(
        self,
        parent_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 100,
        supports_all_drives: bool = True,
        include_items_from_all_drives: bool = True,
    ) -> DriveListPage: ...


def _kind_for_mime(mime_type: str) -> ResourceKind:
    if mime_type == FOLDER_MIME:
        return ResourceKind.FOLDER
    if mime_type == "application/vnd.google-apps.spreadsheet":
        return ResourceKind.SPREADSHEET
    if mime_type == SHORTCUT_MIME:
        return ResourceKind.SHORTCUT
    if mime_type.startswith("application/vnd.google-apps."):
        return ResourceKind.FILE
    return ResourceKind.FILE


def _parse_app_properties(item: Mapping[str, Any]) -> dict[str, str]:
    raw = item.get("appProperties") or {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _parse_properties(item: Mapping[str, Any]) -> dict[str, str]:
    raw = item.get("properties") or {}
    if not isinstance(raw, Mapping):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _resource_from_api(
    item: Mapping[str, Any],
    *,
    parent_id: str | None,
    path: tuple[str, ...],
    depth: int,
    root_id: str,
) -> DriveResource:
    mime = str(item.get("mimeType", "application/octet-stream"))
    size_raw = item.get("size")
    size = int(size_raw) if size_raw is not None else None
    shortcut = item.get("shortcutDetails") or {}
    target_id = shortcut.get("targetId") if isinstance(shortcut, Mapping) else None
    target_mime = (
        shortcut.get("targetMimeType") if isinstance(shortcut, Mapping) else None
    )
    app_properties = _parse_app_properties(item)
    gdm_project = app_properties.get("gdm_project")
    gdm_model = app_properties.get("gdm_model")
    gdm_schema = app_properties.get("gdm_schema")
    name = str(item.get("name", ""))
    is_gdm_folder = name == ".gdm" and mime == FOLDER_MIME
    return DriveResource(
        id=str(item["id"]),
        name=name,
        mime_type=mime,
        kind=_kind_for_mime(mime),
        parent_id=parent_id,
        size=size,
        shared_drive_id=item.get("driveId"),
        path=path,
        shortcut_target_id=str(target_id) if target_id else None,
        shortcut_target_mime=str(target_mime) if target_mime else None,
        app_properties=app_properties,
        properties=_parse_properties(item),
        has_gdm_metadata=is_gdm_folder or bool(gdm_project or gdm_model),
        gdm_project=gdm_project,
        gdm_model=gdm_model,
        gdm_schema=gdm_schema,
        depth=depth,
        is_direct_child_of_root=parent_id == root_id,
    )


class Explorer:
    """Walk Drive roots and build a read-only inventory."""

    def __init__(
        self,
        service: DriveExplorerService,
        *,
        page_size: int = 100,
        supports_all_drives: bool = True,
        include_items_from_all_drives: bool = True,
        max_resources: int | None = None,
    ) -> None:
        self.service = service
        self.page_size = page_size
        self.supports_all_drives = supports_all_drives
        self.include_items_from_all_drives = include_items_from_all_drives
        self.max_resources = max_resources
        self._list_calls = 0

    @property
    def list_call_count(self) -> int:
        """Number of Drive list operations performed (for no-network tests)."""
        return self._list_calls

    def explore(self, root_id: str, *, root_name: str = "") -> ExploreReport:
        """Build an inventory starting at ``root_id`` without mutating Drive."""
        resources: list[DriveResource] = []
        truncated = False
        queue: list[tuple[str, tuple[str, ...], int]] = [
            (root_id, (root_name,) if root_name else (), 0)
        ]
        runtime_notes: list[str] = []
        saw_shortcut_without_target = False

        while queue:
            parent_id, path, depth = queue.pop(0)
            page_token: str | None = None
            while True:
                self._list_calls += 1
                page = self.service.list_children(
                    parent_id,
                    page_token=page_token,
                    page_size=self.page_size,
                    supports_all_drives=self.supports_all_drives,
                    include_items_from_all_drives=self.include_items_from_all_drives,
                )
                for item in page.items:
                    name = str(item.get("name", ""))
                    child_path = (*path, name) if name else path
                    resource = _resource_from_api(
                        item,
                        parent_id=parent_id,
                        path=child_path,
                        depth=depth,
                        root_id=root_id,
                    )
                    if (
                        resource.kind is ResourceKind.SHORTCUT
                        and resource.shortcut_target_id is None
                    ):
                        saw_shortcut_without_target = True
                    resources.append(resource)
                    if (
                        self.max_resources is not None
                        and len(resources) >= self.max_resources
                    ):
                        truncated = True
                        queue.clear()
                        break
                    if resource.kind is ResourceKind.FOLDER:
                        queue.append((resource.id, child_path, depth + 1))
                if truncated:
                    break
                page_token = page.next_page_token
                if not page_token:
                    break
            if truncated:
                break

        direct_count = sum(1 for r in resources if r.is_direct_child_of_root)
        nested_count = len(resources) - direct_count
        mime_dist = analyze_mime_distribution(resources)
        inventory = ExploreInventory(
            root_id=root_id,
            resources=tuple(resources),
            duplicates=_find_duplicate_names(resources),
            type_candidates=_build_type_candidates(resources),
            collection_candidates=analyze_collection_candidates(
                resources, root_id=root_id
            ),
            collection_boundaries=collection_boundaries(resources),
            mime_distribution=dict(mime_dist.counts),
            direct_count=direct_count,
            nested_count=nested_count,
            total_count=len(resources),
            total_size=sum(r.size or 0 for r in resources),
            truncated=truncated,
            zero_content_reads=True,
        )
        warnings = _build_warnings(inventory)
        notes = (
            ZERO_CONTENT_READ_GUARANTEE,
            "Duplicate names require immutable IDs before adoption.",
            "Folder candidates use direct-child MIME distributions for inference.",
        )
        if saw_shortcut_without_target:
            runtime_notes.append(
                "DriveExplorerAdapter lists id,name,mimeType,size,driveId,parents; "
                "shortcut targets and appProperties require extended list fields in "
                "google_api.DriveExplorerAdapter (not owned by this module)."
            )
        return ExploreReport(
            inventory=inventory,
            warnings=warnings,
            notes=notes,
            runtime_integration_notes=tuple(runtime_notes),
        )


def _find_duplicate_names(
    resources: Sequence[DriveResource],
) -> tuple[DuplicateNameGroup, ...]:
    by_parent: dict[tuple[str | None, str], list[str]] = defaultdict(list)
    for resource in resources:
        key = (resource.parent_id, resource.name)
        by_parent[key].append(resource.id)
    groups: list[DuplicateNameGroup] = []
    for (parent_id, name), ids in sorted(by_parent.items()):
        if len(ids) > 1:
            groups.append(
                DuplicateNameGroup(
                    parent_id=parent_id,
                    name=name,
                    resource_ids=tuple(sorted(ids)),
                )
            )
    return tuple(groups)


def _build_type_candidates(
    resources: Sequence[DriveResource],
) -> tuple[TypeCandidate, ...]:
    stats: dict[tuple[str, ResourceKind], dict[str, Any]] = {}
    for resource in resources:
        key = (resource.mime_type, resource.kind)
        bucket = stats.setdefault(
            key,
            {
                "count": 0,
                "total_size": 0,
                "names": [],
                "category": categorize_mime(resource.mime_type),
            },
        )
        bucket["count"] += 1
        bucket["total_size"] += resource.size or 0
        if len(bucket["names"]) < 5:
            bucket["names"].append(resource.name)
    candidates: list[TypeCandidate] = []
    for (mime_type, kind), bucket in sorted(stats.items()):
        candidates.append(
            TypeCandidate(
                mime_type=mime_type,
                count=int(bucket["count"]),
                total_size=int(bucket["total_size"]),
                kind=kind,
                category=bucket["category"],
                sample_names=tuple(bucket["names"]),
            )
        )
    return tuple(candidates)


def _build_warnings(inventory: ExploreInventory) -> tuple[str, ...]:
    warnings: list[str] = []
    if inventory.duplicates:
        warnings.append(
            f"Found {len(inventory.duplicates)} duplicate name group(s) with IDs."
        )
    if inventory.truncated:
        warnings.append("Inventory truncated by max_resources limit.")
    sheet_count = inventory.mime_distribution.get(MimeCategory.SHEET, 0)
    if sheet_count:
        warnings.append(
            f"Detected {sheet_count} spreadsheet(s); bind as sheet models separately."
        )
    gdm_folders = sum(
        1
        for r in inventory.resources
        if r.name == ".gdm" and r.kind is ResourceKind.FOLDER
    )
    if gdm_folders:
        warnings.append(
            f"Found {gdm_folders} existing .gdm metadata folder(s) in inventory."
        )
    return tuple(warnings)


@dataclass
class InMemoryDrivePage:
    """Simple paginated in-memory Drive page."""

    items: Sequence[Mapping[str, Any]]
    next_page_token: str | None = None


@dataclass
class InMemoryDriveService:
    """In-memory Drive explorer service for tests."""

    children: dict[str, list[Mapping[str, Any]]]
    call_log: list[str] = field(default_factory=list)

    def list_children(
        self,
        parent_id: str,
        *,
        page_token: str | None = None,
        page_size: int = 100,
        supports_all_drives: bool = True,
        include_items_from_all_drives: bool = True,
    ) -> InMemoryDrivePage:
        self.call_log.append(f"files.list:{parent_id}")
        del supports_all_drives, include_items_from_all_drives
        items = self.children.get(parent_id, [])
        start = int(page_token or 0)
        end = start + page_size
        chunk = items[start:end]
        next_token = str(end) if end < len(items) else None
        return InMemoryDrivePage(items=chunk, next_page_token=next_token)


def build_resource_tree(
    resources: Iterable[DriveResource],
) -> dict[str | None, list[DriveResource]]:
    """Organize resources by parent ID."""
    tree: dict[str | None, list[DriveResource]] = defaultdict(list)
    for resource in resources:
        tree[resource.parent_id].append(resource)
    for children in tree.values():
        children.sort(key=lambda r: r.name.lower())
    return dict(tree)


def inventory_to_dict(report: ExploreReport) -> dict[str, Any]:
    """Serialize an explore report for CLI JSON output."""
    inv = report.inventory
    return {
        "collection_candidates": [
            {
                "direct_child_count": c.direct_child_count,
                "duplicate_name_groups": c.duplicate_name_groups,
                "folder_id": c.folder_id,
                "gdm_model": c.gdm_model,
                "gdm_project": c.gdm_project,
                "has_gdm_metadata": c.has_gdm_metadata,
                "mime_distribution": c.mime_distribution.to_dict(),
                "name": c.name,
                "nested_descendant_count": c.nested_descendant_count,
                "path": list(c.path),
                "shortcut_count": c.shortcut_count,
            }
            for c in inv.collection_candidates
        ],
        "content_read_guarantee": inv.content_read_guarantee,
        "direct_count": inv.direct_count,
        "duplicates": [
            {
                "name": d.name,
                "parent_id": d.parent_id,
                "resource_ids": list(d.resource_ids),
            }
            for d in inv.duplicates
        ],
        "likely_drive_field_mappings": list(LIKELY_DRIVE_FIELD_MAPPINGS),
        "mime_distribution": {
            cat.value: count for cat, count in inv.mime_distribution.items()
        },
        "nested_count": inv.nested_count,
        "resources": [
            {
                "gdm_model": r.gdm_model,
                "gdm_project": r.gdm_project,
                "has_gdm_metadata": r.has_gdm_metadata,
                "id": r.id,
                "mime_type": r.mime_type,
                "name": r.name,
                "parent_id": r.parent_id,
                "path": list(r.path),
                "shortcut_target_id": r.shortcut_target_id,
                "shortcut_target_mime": r.shortcut_target_mime,
            }
            for r in inv.resources
        ],
        "root_id": inv.root_id,
        "runtime_integration_notes": list(report.runtime_integration_notes),
        "total_count": inv.total_count,
        "total_size": inv.total_size,
        "truncated": inv.truncated,
        "warnings": list(report.warnings),
        "zero_content_reads": inv.zero_content_reads,
    }
