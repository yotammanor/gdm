"""Sidecar JSON indexes with canonical keys and stale detection."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

INDEX_FORMAT_VERSION = 1


class StalePolicy(StrEnum):
    """Behavior when an index fingerprint does not match the live source."""

    FALLBACK = "fallback"
    ERROR = "error"
    REBUILD = "rebuild"


class GdmIndexError(Exception):
    """Raised when index operations fail."""


class StaleIndexError(GdmIndexError):
    """Raised when stale detection policy is ERROR."""


@dataclass(frozen=True, slots=True)
class CanonicalValue:
    """Type-tagged canonical representation of an indexed value."""

    tag: str
    value: str

    def key(self) -> str:
        return f"{self.tag}:{self.value}"


@dataclass(frozen=True, slots=True)
class IndexFingerprints:
    """Separate fingerprints for Drive metadata, metadata table, and content."""

    drive_fingerprint: str = ""
    metadata_fingerprint: str = ""
    content_fingerprint: str = ""
    content_indexed: bool = False

    def combined(self) -> str:
        payload = {
            "content": self.content_fingerprint if self.content_indexed else None,
            "drive": self.drive_fingerprint,
            "metadata": self.metadata_fingerprint,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass
class SidecarIndex:
    """In-memory sidecar index for one model field."""

    model_name: str
    field_name: str
    unique: bool
    source_fingerprint: str
    entries: dict[str, list[str]] = field(default_factory=dict)
    format_version: int = INDEX_FORMAT_VERSION
    locator_kind: str = "drive_file_id"
    fingerprints: IndexFingerprints | None = None
    content_indexed: bool = False

    def lookup(self, value: Any) -> tuple[str, ...]:
        """Return Drive file IDs matching a canonical value."""
        key = canonical_key(value).key()
        return tuple(self.entries.get(key, []))

    def add(self, value: Any, record_id: str) -> None:
        """Add a record ID for a value, enforcing optimistic uniqueness."""
        key = canonical_key(value).key()
        ids = self.entries.setdefault(key, [])
        if record_id in ids:
            return
        if self.unique and ids and ids[0] != record_id:
            raise GdmIndexError(
                f"Unique index violation for {self.model_name}.{self.field_name} "
                f"on value {key!r}."
            )
        ids.append(record_id)

    def remove(self, value: Any, record_id: str) -> None:
        key = canonical_key(value).key()
        ids = self.entries.get(key)
        if not ids:
            return
        if record_id in ids:
            ids.remove(record_id)
        if not ids:
            del self.entries[key]

    def replace_record(
        self,
        old_value: Any,
        new_value: Any,
        record_id: str,
    ) -> None:
        self.remove(old_value, record_id)
        self.add(new_value, record_id)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "content_indexed": self.content_indexed,
            "entries": {k: sorted(v) for k, v in sorted(self.entries.items())},
            "field": self.field_name,
            "format_version": self.format_version,
            "locator_kind": self.locator_kind,
            "model": self.model_name,
            "source_fingerprint": self.source_fingerprint,
            "unique": self.unique,
        }
        if self.fingerprints is not None:
            payload["fingerprints"] = {
                "content": self.fingerprints.content_fingerprint,
                "content_indexed": self.fingerprints.content_indexed,
                "drive": self.fingerprints.drive_fingerprint,
                "metadata": self.fingerprints.metadata_fingerprint,
            }
        return payload


def canonical_key(value: Any) -> CanonicalValue:
    """Convert a value to a deterministic type-tagged canonical key."""
    if value is None:
        return CanonicalValue("n", "null")
    if isinstance(value, bool):
        return CanonicalValue("b", "true" if value else "false")
    if isinstance(value, int) and not isinstance(value, bool):
        return CanonicalValue("i", str(value))
    if isinstance(value, float):
        text = format(value, ".15g")
        return CanonicalValue("f", text)
    if isinstance(value, (list, dict, tuple, set)):
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
        tag = "a" if isinstance(value, list) else "o"
        return CanonicalValue(tag, digest)
    text = str(value).strip()
    return CanonicalValue("s", text)


def compute_source_fingerprint(
    *,
    resource_id: str,
    version: str | int | None = None,
    row_count: int | None = None,
    checksum: str | None = None,
    drive_fingerprint: str | None = None,
    metadata_fingerprint: str | None = None,
    content_fingerprint: str | None = None,
    content_indexed: bool = False,
) -> str:
    """Compute a source fingerprint for stale detection."""
    if any(
        value is not None
        for value in (drive_fingerprint, metadata_fingerprint, content_fingerprint)
    ):
        fps = IndexFingerprints(
            drive_fingerprint=drive_fingerprint or "",
            metadata_fingerprint=metadata_fingerprint or "",
            content_fingerprint=content_fingerprint or "",
            content_indexed=content_indexed,
        )
        return fps.combined()
    payload = {
        "checksum": checksum,
        "resource_id": resource_id,
        "row_count": row_count,
        "version": version,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compute_collection_fingerprints(
    *,
    folder_id: str,
    drive_version: str | int | None = None,
    metadata_table_fingerprint: str | None = None,
    content_fingerprint: str | None = None,
    content_indexed: bool = False,
) -> IndexFingerprints:
    """Build separate fingerprints for a collection model index."""
    drive_fp = compute_source_fingerprint(resource_id=folder_id, version=drive_version)
    metadata_fp = metadata_table_fingerprint or ""
    content_fp = content_fingerprint or ""
    return IndexFingerprints(
        drive_fingerprint=drive_fp,
        metadata_fingerprint=metadata_fp,
        content_fingerprint=content_fp,
        content_indexed=content_indexed,
    )


def serialize_index(index: SidecarIndex) -> str:
    """Serialize an index to deterministic JSON."""
    return json.dumps(index.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"


def deserialize_index(raw: str) -> SidecarIndex:
    """Load an index from JSON text."""
    data = json.loads(raw)
    entries_raw = data.get("entries", {})
    entries = {str(k): list(v) for k, v in entries_raw.items()}
    fps_raw = data.get("fingerprints")
    fingerprints = None
    if isinstance(fps_raw, dict):
        fingerprints = IndexFingerprints(
            drive_fingerprint=str(fps_raw.get("drive", "")),
            metadata_fingerprint=str(fps_raw.get("metadata", "")),
            content_fingerprint=str(fps_raw.get("content", "")),
            content_indexed=bool(fps_raw.get("content_indexed", False)),
        )
    return SidecarIndex(
        model_name=str(data["model"]),
        field_name=str(data["field"]),
        unique=bool(data.get("unique", False)),
        source_fingerprint=str(data.get("source_fingerprint", "")),
        entries=entries,
        format_version=int(data.get("format_version", INDEX_FORMAT_VERSION)),
        locator_kind=str(data.get("locator_kind", "drive_file_id")),
        fingerprints=fingerprints,
        content_indexed=bool(data.get("content_indexed", False)),
    )


def verify_fingerprint(
    index: SidecarIndex,
    live_fingerprint: str,
    *,
    policy: StalePolicy = StalePolicy.FALLBACK,
) -> SidecarIndex | None:
    """Verify index freshness and apply stale policy."""
    if index.source_fingerprint == live_fingerprint:
        return index
    if policy is StalePolicy.ERROR:
        raise StaleIndexError(
            f"Index for {index.model_name}.{index.field_name} is stale."
        )
    if policy is StalePolicy.REBUILD:
        return SidecarIndex(
            model_name=index.model_name,
            field_name=index.field_name,
            unique=index.unique,
            source_fingerprint=live_fingerprint,
            entries={},
        )
    return None


@dataclass
class IndexStore:
    """Filesystem-backed index store under ``.gdm/indexes``."""

    indexes_dir: str

    def path_for(self, model_name: str, field_name: str) -> str:
        safe_model = model_name.replace("/", "_")
        safe_field = field_name.replace("/", "_")
        return f"{self.indexes_dir}/{safe_model}/{safe_field}.json"

    def load(self, model_name: str, field_name: str) -> SidecarIndex | None:
        import os

        path = self.path_for(model_name, field_name)
        if not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return deserialize_index(handle.read())

    def save(self, index: SidecarIndex) -> str:
        import os

        path = self.path_for(index.model_name, index.field_name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(serialize_index(index))
        return path

    def delete(self, model_name: str, field_name: str) -> bool:
        import os

        path = self.path_for(model_name, field_name)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False


@dataclass
class IndexMaintainer:
    """Incremental index maintenance helpers."""

    store: IndexStore

    def rebuild_from_records(
        self,
        *,
        model_name: str,
        field_name: str,
        unique: bool,
        records: Iterable[tuple[str, Mapping[str, Any]]],
        source_fingerprint: str,
        fingerprints: IndexFingerprints | None = None,
        content_indexed: bool = False,
        locator_kind: str = "drive_file_id",
    ) -> SidecarIndex:
        index = SidecarIndex(
            model_name=model_name,
            field_name=field_name,
            unique=unique,
            source_fingerprint=source_fingerprint,
            fingerprints=fingerprints,
            content_indexed=content_indexed,
            locator_kind=locator_kind,
        )
        for record_id, payload in records:
            if field_name not in payload:
                continue
            index.add(payload[field_name], record_id)
        self.store.save(index)
        return index

    def update_record(
        self,
        index: SidecarIndex,
        *,
        record_id: str,
        old_payload: Mapping[str, Any] | None,
        new_payload: Mapping[str, Any],
    ) -> SidecarIndex:
        field = index.field_name
        if old_payload and field in old_payload:
            index.remove(old_payload[field], record_id)
        if field in new_payload:
            index.add(new_payload[field], record_id)
        self.store.save(index)
        return index

    def lookup_with_policy(
        self,
        model_name: str,
        field_name: str,
        value: Any,
        *,
        live_fingerprint: str,
        policy: StalePolicy = StalePolicy.FALLBACK,
    ) -> tuple[str, ...] | None:
        index = self.store.load(model_name, field_name)
        if index is None:
            return None
        verified = verify_fingerprint(index, live_fingerprint, policy=policy)
        if verified is None:
            return None
        if verified is not index and policy is StalePolicy.REBUILD:
            self.store.save(verified)
            return ()
        return verified.lookup(value)


def merge_index_entries(
    left: SidecarIndex,
    right: SidecarIndex,
) -> SidecarIndex:
    """Merge two indexes deterministically (used in tests)."""
    if left.model_name != right.model_name or left.field_name != right.field_name:
        msg = "Cannot merge indexes for different model fields."
        raise GdmIndexError(msg)
    merged_entries: dict[str, list[str]] = {}
    for source in (left, right):
        for key, ids in source.entries.items():
            merged_entries.setdefault(key, [])
            for record_id in ids:
                if record_id not in merged_entries[key]:
                    merged_entries[key].append(record_id)
            merged_entries[key].sort()
    fingerprint = left.source_fingerprint or right.source_fingerprint
    return SidecarIndex(
        model_name=left.model_name,
        field_name=left.field_name,
        unique=left.unique or right.unique,
        source_fingerprint=fingerprint,
        entries=merged_entries,
    )


def field_is_indexed(model: type[Any], field_name: str) -> bool:
    """Return whether ``field_name`` participates in sidecar indexes."""
    from gdm.model import GDMModel

    if not issubclass(model, GDMModel):
        return False
    info = model.model_fields.get(field_name)
    if info is None:
        return False
    from gdm.fields import get_gdm_field_metadata

    metadata = get_gdm_field_metadata(info)
    return metadata.index or metadata.unique


def indexed_fields_for_model(model: type[Any]) -> tuple[tuple[str, bool], ...]:
    """Return ``(field_name, unique)`` pairs for indexed model fields."""
    from gdm.model import GDMModel

    if not issubclass(model, GDMModel):
        return ()
    from gdm.fields import get_gdm_field_metadata

    fields: list[tuple[str, bool]] = []
    for name, info in model.model_fields.items():
        metadata = get_gdm_field_metadata(info)
        if metadata.index or metadata.unique:
            fields.append((name, metadata.unique))
    return tuple(fields)


def locator_kind_for_model(model: type[Any]) -> str:
    """Return the sidecar locator kind for ``model``."""
    from gdm.model import GDMModel

    if not issubclass(model, GDMModel):
        return "record_key"
    if model.is_collection_model():
        return "drive_file_id"
    if model.gdm_storage() == "sheet":
        return "sheet_row_pk"
    return "record_key"


def record_payload_for_index(
    model: type[Any],
    stored: Any,
) -> dict[str, Any]:
    """Build a field-name keyed payload from a stored record."""
    from gdm.model import GDMModel

    if not issubclass(model, GDMModel):
        return dict(stored.values)
    mapped = dict(stored.values)
    source_to_field = model.source_field_map()
    for source_key, field_name in source_to_field.items():
        if source_key in stored.values and field_name not in mapped:
            mapped[field_name] = stored.values[source_key]
    return mapped


@dataclass
class IndexRuntime:
    """Optional runtime index store wired through client, registry, or session."""

    store: IndexStore
    stale_policy: StalePolicy = StalePolicy.FALLBACK

    @property
    def maintainer(self) -> IndexMaintainer:
        return IndexMaintainer(self.store)

    def lookup_record_ids(
        self,
        model: type[Any],
        field_name: str,
        value: Any,
        backend: Any,
    ) -> tuple[str, ...] | None:
        """Resolve record IDs via sidecar index, or ``None`` to fall back to scan."""
        live_fingerprint = compute_live_fingerprint(backend, model)
        ids = self.maintainer.lookup_with_policy(
            model.gdm_model_name(),
            field_name,
            value,
            live_fingerprint=live_fingerprint,
            policy=self.stale_policy,
        )
        if ids is None:
            return None
        if ids == () and self.stale_policy is StalePolicy.REBUILD:
            self.rebuild_field_index(model, field_name, backend)
            index = self.store.load(model.gdm_model_name(), field_name)
            if index is None:
                return ()
            return index.lookup(value)
        return ids

    def rebuild_field_index(
        self,
        model: type[Any],
        field_name: str,
        backend: Any,
    ) -> SidecarIndex:
        """Rebuild one field index from the live backend."""
        unique = _field_unique(model, field_name)
        before_fingerprint, _ = compute_live_fingerprint_parts(backend, model)
        records = _collect_records_for_index(model, backend)
        fingerprint, fingerprints = compute_live_fingerprint_parts(backend, model)
        if before_fingerprint != fingerprint:
            raise StaleIndexError(
                f"{model.gdm_model_name()} changed while rebuilding "
                f"index {field_name!r}; retry the rebuild."
            )
        return self.maintainer.rebuild_from_records(
            model_name=model.gdm_model_name(),
            field_name=field_name,
            unique=unique,
            records=records,
            source_fingerprint=fingerprint,
            fingerprints=fingerprints,
            content_indexed=False,
            locator_kind=locator_kind_for_model(model),
        )

    def rebuild_model_indexes(
        self,
        model: type[Any],
        backend: Any,
    ) -> dict[str, int]:
        """Rebuild every indexed field for ``model``."""
        counts: dict[str, int] = {}
        for field_name, _unique in indexed_fields_for_model(model):
            index = self.rebuild_field_index(model, field_name, backend)
            counts[field_name] = sum(len(ids) for ids in index.entries.values())
        return counts

    def refresh_model_fingerprints(
        self,
        model: type[Any],
        backend: Any,
    ) -> None:
        """Update stored fingerprints after incremental index maintenance."""
        fingerprint, fingerprints = compute_live_fingerprint_parts(backend, model)
        model_name = model.gdm_model_name()
        for field_name, _unique in indexed_fields_for_model(model):
            index = self.store.load(model_name, field_name)
            if index is None:
                continue
            index.source_fingerprint = fingerprint
            index.fingerprints = fingerprints
            self.store.save(index)

    def validate_unique_constraints(
        self,
        *,
        model: type[Any],
        pending: Iterable[tuple[str, Mapping[str, Any] | None, Mapping[str, Any]]],
        backend: Any,
    ) -> None:
        """Raise :class:`GdmIndexError` when unique fields would collide."""
        validate_unique_constraints(model=model, pending=pending, backend=backend)

    def maintain_after_write(
        self,
        *,
        model: type[Any],
        record_id: str,
        old_payload: Mapping[str, Any] | None,
        new_payload: Mapping[str, Any],
        backend: Any,
    ) -> None:
        """Incrementally update sidecar indexes for one successful write."""
        model_name = model.gdm_model_name()
        for field_name, unique in indexed_fields_for_model(model):
            index = self.store.load(model_name, field_name)
            if index is None:
                index = SidecarIndex(
                    model_name=model_name,
                    field_name=field_name,
                    unique=unique,
                    source_fingerprint="",
                    locator_kind=locator_kind_for_model(model),
                )
            self.maintainer.update_record(
                index,
                record_id=record_id,
                old_payload=old_payload,
                new_payload=new_payload,
            )
        self.refresh_model_fingerprints(model, backend)

    def maintain_after_delete(
        self,
        *,
        model: type[Any],
        record_id: str,
        old_payload: Mapping[str, Any],
        backend: Any,
    ) -> None:
        """Remove a deleted record from sidecar indexes."""
        model_name = model.gdm_model_name()
        for field_name, _unique in indexed_fields_for_model(model):
            index = self.store.load(model_name, field_name)
            if index is None:
                continue
            self.maintainer.update_record(
                index,
                record_id=record_id,
                old_payload=old_payload,
                new_payload={},
            )
        self.refresh_model_fingerprints(model, backend)


def validate_unique_constraints(
    *,
    model: type[Any],
    pending: Iterable[tuple[str, Mapping[str, Any] | None, Mapping[str, Any]]],
    backend: Any,
) -> None:
    """Raise :class:`GdmIndexError` when unique fields would collide."""
    unique_fields = [
        name for name, is_unique in indexed_fields_for_model(model) if is_unique
    ]
    if not unique_fields:
        return
    for field_name in unique_fields:
        seen: dict[str, str] = {}
        for record_id, _old_payload, new_payload in pending:
            if field_name not in new_payload:
                continue
            value = new_payload[field_name]
            if value is None:
                continue
            key = canonical_key(value).key()
            if key in seen and seen[key] != record_id:
                raise GdmIndexError(
                    f"Unique constraint violation on {model.gdm_model_name()}."
                    f"{field_name} for value {key!r}."
                )
            seen[key] = record_id
        for record_id, value in _persisted_unique_values(model, field_name, backend):
            key = canonical_key(value).key()
            if key in seen and seen[key] != record_id:
                raise GdmIndexError(
                    f"Unique constraint violation on {model.gdm_model_name()}."
                    f"{field_name} for value {key!r}."
                )


def _field_unique(model: type[Any], field_name: str) -> bool:
    from gdm.fields import get_gdm_field_metadata
    from gdm.model import GDMModel

    if not issubclass(model, GDMModel):
        return False
    info = model.model_fields.get(field_name)
    if info is None:
        return False
    return get_gdm_field_metadata(info).unique


def _collect_records_for_index(
    model: type[Any],
    backend: Any,
) -> list[tuple[str, Mapping[str, Any]]]:
    limit = getattr(backend, "scan_limit", None)
    records = backend.scan(model, limit=limit)
    collected: list[tuple[str, Mapping[str, Any]]] = []
    for stored in records:
        payload = record_payload_for_index(model, stored)
        collected.append((stored.locator.record_key, payload))
    return collected


def _persisted_unique_values(
    model: type[Any],
    field_name: str,
    backend: Any,
) -> list[tuple[str, Any]]:
    index_runtime = getattr(backend, "index_runtime", None)
    if index_runtime is not None:
        index = index_runtime.store.load(model.gdm_model_name(), field_name)
        if index is not None and index.source_fingerprint == compute_live_fingerprint(
            backend, model
        ):
            values: list[tuple[str, Any]] = []
            for key, ids in index.entries.items():
                decoded = decode_index_key(key)
                for record_id in ids:
                    values.append((record_id, decoded))
            return values
    values = []
    for stored in backend.scan(model, limit=getattr(backend, "scan_limit", None)):
        payload = record_payload_for_index(model, stored)
        if field_name in payload and payload[field_name] is not None:
            values.append((stored.locator.record_key, payload[field_name]))
    return values


def decode_index_key(key: str) -> Any:
    """Decode a canonical index key back to a Python value."""
    tag, _, raw = key.partition(":")
    if tag == "s":
        return raw
    if tag == "i":
        return int(raw)
    if tag == "f":
        return float(raw)
    if tag == "b":
        return raw == "true"
    if tag == "n":
        return None
    return raw


def compute_live_fingerprint(backend: Any, model: type[Any]) -> str:
    """Return the combined live fingerprint for ``model``."""
    fingerprint, _fingerprints = compute_live_fingerprint_parts(backend, model)
    return fingerprint


def compute_live_fingerprint_parts(
    backend: Any,
    model: type[Any],
) -> tuple[str, IndexFingerprints | None]:
    """Return combined and component fingerprints for ``model``."""
    compute_fn = getattr(backend, "compute_index_fingerprints", None)
    if callable(compute_fn):
        return compute_fn(model)
    resource_id = getattr(model, "__gdm_source__", None) or model.gdm_model_name()
    row_count = len(backend.scan(model, limit=getattr(backend, "scan_limit", None)))
    fingerprint = compute_source_fingerprint(
        resource_id=str(resource_id),
        row_count=row_count,
    )
    return fingerprint, None
