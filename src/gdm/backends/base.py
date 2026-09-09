"""Backend protocols and transport-neutral record types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

StorageKind = Literal["file", "sheet", "collection"]


@dataclass(frozen=True, slots=True)
class RecordLocator:
    """Stable address for a stored record."""

    storage: StorageKind
    resource_id: str
    record_key: str


@dataclass(slots=True)
class StoredRecord:
    """Transport-neutral stored record payload."""

    locator: RecordLocator
    values: dict[str, Any]
    unknown: dict[str, Any] = field(default_factory=dict)
    version: str | None = None
    fingerprint: str | None = None
    drive_version: str | None = None
    drive_fingerprint: str | None = None
    metadata_version: str | None = None
    metadata_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Result of a successful write."""

    locator: RecordLocator
    version: str
    fingerprint: str
    created: bool = False
    metadata_version: str | None = None
    metadata_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class DeleteResult:
    """Result of a successful delete."""

    locator: RecordLocator


@dataclass(frozen=True, slots=True)
class BackendCapabilities:
    """Advertised backend capabilities."""

    supports_scan: bool = True
    supports_metadata_pushdown: bool = False
    supports_sidecar_index: bool = False
    supports_batch_write: bool = False
    supports_conditional_write: bool = True


@dataclass(frozen=True, slots=True)
class ModelSchemaInfo:
    """Schema snapshot for a mapped model."""

    model_name: str
    storage: StorageKind
    source: str | None
    mapping: str
    schema_version: str | None
    field_sources: tuple[tuple[str, str], ...]


@runtime_checkable
class Backend(Protocol):
    """Protocol implemented by Drive, Sheets, and test backends."""

    def capabilities(self) -> BackendCapabilities:
        """Return backend capability flags."""

    def inspect_schema(self, model: type[Any]) -> ModelSchemaInfo:
        """Return schema information for ``model``."""

    def read(self, model: type[Any], locator: RecordLocator) -> StoredRecord:
        """Load one record."""

    def scan(
        self,
        model: type[Any],
        *,
        limit: int | None = None,
    ) -> list[StoredRecord]:
        """Scan all records for ``model``."""

    def write(
        self,
        model: type[Any],
        record: StoredRecord,
        *,
        expected_version: str | None,
        create: bool = False,
    ) -> WriteResult:
        """Create or update a record with optimistic concurrency."""

    def delete(
        self,
        model: type[Any],
        locator: RecordLocator,
        *,
        expected_version: str | None,
    ) -> DeleteResult:
        """Delete a record with optimistic concurrency."""
