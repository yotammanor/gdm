"""Typed Drive folder collection API."""

from __future__ import annotations

import builtins
from collections.abc import Mapping
from typing import Any, Generic, TypeVar, cast

from gdm.collection.backend import CollectionBackend, collection_drive_fingerprint
from gdm.collection.constants import FOLDER_MIME
from gdm.collection.content import ContentHandle
from gdm.collection.inventory import DriveInventoryItem, mime_matches
from gdm.collection.metadata import CollectionMetadataStore
from gdm.errors import (
    AmbiguousResourceError,
    InvalidModelConfigError,
    NotFoundError,
    ScanLimitExceeded,
)
from gdm.fields import source_name
from gdm.model import GDMModel

T = TypeVar("T", bound=GDMModel)


def collection_model_with_mime(model: type[T], pattern: str) -> type[T]:
    """Return a collection model subclass filtered by an additional MIME pattern.

    Prefer :meth:`DriveCollection.where_mime`, which keeps the base model class
    intact. Dynamic Pydantic subclasses break GDM ClassVars and instance binding.
    """
    patterns = (*model.__gdm_mime_patterns__, pattern)  # type: ignore[attr-defined]
    filtered = type(
        f"{model.__name__}MimeFilter",
        (model,),
        {"__gdm_mime_patterns__": patterns},
    )
    filtered.__gdm_storage__ = model.__gdm_storage__
    filtered.__gdm_source__ = model.__gdm_source__
    filtered.__gdm_collection__ = model.__gdm_collection__
    filtered.__gdm_mapping__ = model.__gdm_mapping__
    filtered.__gdm_schema_version__ = model.__gdm_schema_version__
    filtered.__gdm_model_name__ = model.__gdm_model_name__
    filtered.__gdm_codec__ = model.__gdm_codec__
    filtered.__gdm_mime_patterns__ = patterns  # type: ignore[attr-defined]
    return cast(type[T], filtered)


class DriveCollection(Generic[T]):
    """Typed collection bound to one Drive folder."""

    def __init__(
        self,
        *,
        model: type[T],
        folder_id: str,
        backend: CollectionBackend,
        metadata_store: CollectionMetadataStore,
        scan_limit: int = 1000,
        extra_mime_patterns: tuple[str, ...] = (),
    ) -> None:
        self._model = model
        self.folder_id = folder_id
        self._backend = backend
        self._metadata_store = metadata_store
        self.scan_limit = scan_limit
        self._extra_mime_patterns = extra_mime_patterns

    @property
    def model(self) -> type[T]:
        return self._model

    @property
    def mime_patterns(self) -> tuple[str, ...]:
        """Effective MIME selectors including ``where_mime`` filters."""
        base = self._model.__gdm_mime_patterns__  # type: ignore[attr-defined]
        if not self._extra_mime_patterns:
            return base
        return (*base, *self._extra_mime_patterns)

    def inventory(self) -> builtins.list[DriveInventoryItem]:
        """Return all direct children, including unmodeled files."""
        return self._backend.inventory(self.folder_id).scan(
            scan_limit=self.scan_limit,
            include_folders=True,
        )

    def list(self) -> builtins.list[T]:
        """Return typed instances for modeled direct children."""
        return [self._materialize(item) for item in self._iter_modeled()]

    def get(self, file_id: str) -> T:
        item = self._member_item(file_id)
        if not self._matches_item(item):
            msg = (
                f"File {file_id} mime type {item.mime_type!r} does not match "
                f"model {self._model.__name__} selectors."
            )
            raise NotFoundError(msg)
        return self._materialize(item)

    def find_by_name(self, name: str) -> builtins.list[T]:
        items = self._backend.inventory(self.folder_id).find_by_name(name)
        modeled = [item for item in items if self._matches_item(item)]
        return [self._materialize(item) for item in modeled]

    def one_by_name(self, name: str) -> T:
        matches = self.find_by_name(name)
        if not matches:
            msg = f"No modeled file named {name!r} under {self.folder_id}."
            raise NotFoundError(msg)
        if len(matches) > 1:
            ids = [cast(GDMModel, item).gdm_record_key() for item in matches]
            msg = f"Ambiguous file name {name!r}: {ids}"
            raise AmbiguousResourceError(msg)
        return matches[0]

    def where_mime(self, pattern: str) -> DriveCollection[T]:
        return DriveCollection(
            model=self._model,
            folder_id=self.folder_id,
            backend=self._backend,
            metadata_store=self._metadata_store,
            scan_limit=self.scan_limit,
            extra_mime_patterns=(*self._extra_mime_patterns, pattern),
        )

    def iter_recursive(self) -> builtins.list[T]:
        """Return modeled items from nested subfolders (explicit recursion)."""
        results: builtins.list[T] = []
        for item in self.inventory():
            if item.is_folder:
                sub = self.subcollection(item.file_id)
                results.extend(sub.iter_recursive())
            elif self._matches_item(item):
                results.append(self._materialize(item))
        return results

    def subcollection(self, subfolder_id: str) -> DriveCollection[T]:
        item = self._member_item(subfolder_id)
        if not item.is_folder:
            msg = f"Resource {subfolder_id!r} is not a direct child folder."
            raise NotFoundError(msg)
        return DriveCollection(
            model=self._model,
            folder_id=subfolder_id,
            backend=self._backend,
            metadata_store=self._metadata_store,
            scan_limit=self.scan_limit,
            extra_mime_patterns=self._extra_mime_patterns,
        )

    def content(self, file_id: str) -> ContentHandle:
        item = self._member_item(file_id)
        return self._backend.content_handle(item)

    def create_native(
        self,
        name: str,
        kind: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> T:
        metadata_values = self._metadata_values_from_input(metadata)
        self._backend.validate_create_metadata(self._model, metadata_values)
        item = self._backend.create_native(
            folder_id=self.folder_id,
            name=name,
            kind=kind,
            model=self._model,
            metadata_values=metadata_values or None,
        )
        if metadata_values:
            self._metadata_store.upsert_row(item.file_id, metadata_values)
        return self._materialize(item)

    def upload(
        self,
        name: str,
        data: bytes,
        *,
        mime_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> T:
        metadata_values = self._metadata_values_from_input(metadata)
        self._backend.validate_create_metadata(self._model, metadata_values)
        item = self._backend.upload_binary(
            folder_id=self.folder_id,
            name=name,
            data=data,
            mime_type=mime_type,
            model=self._model,
        )
        if metadata_values:
            self._metadata_store.upsert_row(item.file_id, metadata_values)
        return self._materialize(item)

    def adopt(
        self,
        file_id: str,
        *,
        move: bool = False,
        shortcut: bool = False,
    ) -> T:
        item = self._backend.adopt(
            folder_id=self.folder_id,
            file_id=file_id,
            move=move,
            shortcut=shortcut,
        )
        return self._materialize(item)

    def move(
        self,
        file_id: str,
        *,
        to_folder_id: str,
    ) -> T:
        self._member_item(file_id)
        item = self._backend.adopt(
            folder_id=to_folder_id,
            file_id=file_id,
            move=True,
        )
        destination = DriveCollection(
            model=self._model,
            folder_id=to_folder_id,
            backend=self._backend,
            metadata_store=self._metadata_store,
            scan_limit=self.scan_limit,
            extra_mime_patterns=self._extra_mime_patterns,
        )
        return destination._materialize(item)

    def detach(self, file_id: str, *, to_folder_id: str) -> T:
        self._member_item(file_id)
        item = self._backend.detach(
            file_id=file_id,
            collection_folder_id=self.folder_id,
            to_folder_id=to_folder_id,
        )
        destination = DriveCollection(
            model=self._model,
            folder_id=to_folder_id,
            backend=self._backend,
            metadata_store=self._metadata_store,
            scan_limit=self.scan_limit,
            extra_mime_patterns=self._extra_mime_patterns,
        )
        return destination._materialize(item)

    def trash(self, file_id: str) -> None:
        self._member_item(file_id)
        self._backend.trash(file_id)

    def delete_permanently(self, file_id: str) -> None:
        self._member_item(file_id)
        self._backend.delete_permanently(file_id)

    def reconcile_metadata(self) -> tuple[builtins.list[str], builtins.list[str]]:
        file_ids = {item.file_id for item in self.inventory()}
        return self._metadata_store.reconcile(folder_file_ids=file_ids)

    def _matches_item(self, item: DriveInventoryItem) -> bool:
        patterns = self.mime_patterns
        if not patterns:
            return True
        return any(mime_matches(item.mime_type, pattern) for pattern in patterns)

    def _iter_modeled(self) -> builtins.list[DriveInventoryItem]:
        items = self._backend.inventory(self.folder_id).scan(
            scan_limit=self.scan_limit + 1,
            include_folders=False,
        )
        if len(items) > self.scan_limit:
            raise ScanLimitExceeded(
                f"Collection scan exceeded limit {self.scan_limit}.",
                limit=self.scan_limit,
                observed=len(items),
                model_name=self._model.gdm_model_name(),
            )
        return [
            item
            for item in items
            if self._matches_item(item) and item.mime_type != FOLDER_MIME
        ]

    def _materialize(self, item: DriveInventoryItem) -> T:
        row = self._metadata_store.get_row(item.file_id)
        metadata_values = dict(row.values) if row is not None else {}
        mapped, unknown = self._backend.materialize_values(
            self._model,
            item,
            metadata_values,
        )
        instance = self._model.model_validate(mapped)
        from gdm.backends.base import RecordLocator

        instance.gdm_bind_collection(
            locator=RecordLocator(
                storage="collection",
                resource_id=self.folder_id,
                record_key=item.file_id,
            ),
            drive_version=item.version,
            drive_fingerprint=collection_drive_fingerprint(item),
            metadata_version=row.version if row else None,
            metadata_fingerprint=row.fingerprint if row else None,
            unknown_fields=unknown,
            metadata_store=self._metadata_store,
            inventory_item=item,
            collection_backend=self._backend,
            persisted=True,
        )
        return instance

    def _member_item(self, file_id: str) -> DriveInventoryItem:
        item = self._backend.inventory(self.folder_id).get(file_id)
        self._backend.assert_resource_belongs_to_project(item)
        return item

    def _metadata_values_from_input(
        self,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if metadata is None:
            return {}
        values = dict(metadata)
        for name, info in self._model.model_fields.items():
            from gdm.fields import field_storage_target

            target_kind, target_name = field_storage_target(
                name, info, storage=self._model.gdm_storage()
            )
            if target_kind in {"metadata", "source"} and name in values:
                values.setdefault(target_name, values.pop(name))
        unknown_keys = [
            key
            for key in values
            if key
            not in {
                source_name(name, info)
                for name, info in self._model.model_fields.items()
            }
            and key not in self._model.model_fields
        ]
        if unknown_keys and self._model.gdm_mapping() == "strict":
            msg = f"Unknown metadata keys for {self._model.__name__}: {unknown_keys}"
            raise InvalidModelConfigError(msg)
        return values
