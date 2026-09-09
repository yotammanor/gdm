"""Collection adoption planning without implicit content mutation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class AdoptPlan:
    """Inspect-only plan to bind a folder as a collection model."""

    model_name: str
    folder_id: str
    collection_path: str | None
    mapping_mode: str = "partial"
    mime_patterns: tuple[str, ...] = ()
    metadata_only: bool = True
    writes: tuple[str, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_destructive(self) -> bool:
        return any(
            token in write
            for write in self.writes
            for token in ("move", "trash", "delete", "convert", "content")
        )


@dataclass
class AdoptResult:
    """Outcome of an adopt operation."""

    plan: AdoptPlan
    applied: bool
    binding: dict[str, Any] | None = None
    message: str = ""


class AdoptPlanner:
    """Build adoption plans that default to metadata-only binding."""

    def plan_bind_folder(
        self,
        *,
        model_name: str,
        folder_id: str,
        collection_path: str | None = None,
        mapping_mode: str = "partial",
        mime_patterns: tuple[str, ...] = (),
    ) -> AdoptPlan:
        writes = (
            "registry: bind folder ID to model",
            "metadata-store: ensure collection metadata storage exists",
        )
        notes = (
            "Adoption binds folder membership and metadata; it does not download "
            "or rewrite native file content.",
            "Existing members remain untouched; GDM adds routing appProperties only "
            "to members it creates or explicitly retags.",
            "Use explicit move/trash/content APIs for destructive changes.",
        )
        return AdoptPlan(
            model_name=model_name,
            folder_id=folder_id,
            collection_path=collection_path,
            mapping_mode=mapping_mode,
            mime_patterns=mime_patterns,
            metadata_only=True,
            writes=writes,
            notes=notes,
        )

    def apply_plan(
        self,
        plan: AdoptPlan,
        *,
        binder: Any | None = None,
        dry_run: bool = True,
    ) -> AdoptResult:
        if dry_run:
            return AdoptResult(
                plan=plan,
                applied=False,
                message="Dry-run: no Drive or Sheets writes performed.",
            )
        if binder is None:
            return AdoptResult(
                plan=plan,
                applied=False,
                message="No binder configured; wire adopt via GDMClient in production.",
            )
        binding = binder(plan)
        return AdoptResult(
            plan=plan,
            applied=True,
            binding=binding,
            message="Collection binding applied.",
        )
