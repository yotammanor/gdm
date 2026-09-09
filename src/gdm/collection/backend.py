"""Collection backend for registry/session integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gdm.backends.drive import DriveBackend
from gdm.collection.constants import (
    FOLDER_MIME,
    MAX_DRIVE_PROPERTIES_PER_FILE,
    MAX_DRIVE_PROPERTY_BYTES,
    NATIVE_CREATE_MIMES,
    READ_ONLY_DRIVE_FIELDS,
    SHORTCUT_MIME,
)
from gdm.collection.content import ContentHandle
from gdm.collection.inventory import (
    DriveInventory,
    DriveInventoryItem,
    drive_fingerprint,
    inventory_item_from_file,
    mime_matches,
)
from gdm.collection.metadata import CollectionMetadataRow, CollectionMetadataStore
from gdm.errors import (
    ForeignProjectError,
    InvalidDrivePropertyError,
    InvalidModelConfigError,
    UnsupportedOperationError,
)
from gdm.fields import field_storage_target, get_gdm_field_metadata
from gdm.model import GDMModel


@dataclass(frozen=True, slots=True)
class CollectionBinding:
    folder_id: str
    metadata_store: CollectionMetadataStore
    mime_patterns: tuple[str, ...] = ()


class CollectionBackend:
    """Drive folder collection operations without content downloads."""

    DEFAULT_FIELDS = (
        "id,name,mimeType,version,md5Checksum,properties,appProperties,parents,trashed,"
        "description,starred,createdTime,modifiedTime,webViewLink,size,"
        "capabilities,shortcutDetails,resourceKey"
    )

    def __init__(
        self,
        drive: DriveBackend,
        *,
        project_id: str,
        documents_service: Any | None = None,
        slides_service: Any | None = None,
        sheets_service: Any | None = None,
        adoption_mode: bool = False,
        resource_scope_validator: Callable[[str], Any] | None = None,
    ) -> None:
        self.drive = drive
        self.project_id = project_id
        self._documents = documents_service
        self._slides = slides_service
        self._sheets = sheets_service
        self.adoption_mode = adoption_mode
        self._resource_scope_validator = resource_scope_validator

    def inventory(self, folder_id: str) -> DriveInventory:
        return DriveInventory(
            self.drive.service.files,
            folder_id=folder_id,
            list_kwargs=self.drive._list_kwargs,
            call=self.drive._call,
        )

    def materialize_values(
        self,
        model: type[GDMModel],
        item: DriveInventoryItem,
        metadata_values: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.assert_resource_belongs_to_project(item)
        mapped: dict[str, Any] = {}
        unknown_meta: dict[str, Any] = {}
        metadata = dict(metadata_values or {})
        for name, info in model.model_fields.items():
            target_kind, target_name = field_storage_target(
                name, info, storage=model.gdm_storage()
            )
            if target_kind == "drive":
                mapped[name] = self._drive_field_value(item, target_name)
            elif target_kind == "metadata":
                if target_name in metadata:
                    mapped[name] = metadata.pop(target_name)
            elif target_kind in {"property", "app_property"}:
                store = (
                    item.properties
                    if target_kind == "property"
                    else item.app_properties
                )
                mapped[name] = store.get(target_name)
            elif target_kind == "source" and target_name in metadata:
                mapped[name] = metadata.pop(target_name)
        if model.gdm_mapping() != "strict":
            unknown_meta.update(metadata)
        return mapped, unknown_meta

    def validate_create_metadata(
        self,
        model: type[GDMModel],
        metadata_values: dict[str, Any],
    ) -> None:
        """Ensure required metadata fields are present before native creation."""
        missing: list[str] = []
        for name, info in model.model_fields.items():
            target_kind, target_name = field_storage_target(
                name, info, storage=model.gdm_storage()
            )
            if target_kind not in {"metadata", "source"}:
                continue
            if not info.is_required():
                continue
            if target_name in metadata_values or name in metadata_values:
                continue
            missing.append(name)
        if missing:
            msg = (
                f"Missing required metadata for {model.__name__}: "
                f"{', '.join(sorted(missing))}"
            )
            raise InvalidModelConfigError(msg)

    @staticmethod
    def serialize_property_value(value: Any) -> str:
        """Serialize a typed field value to a Drive property string."""
        if value is None:
            msg = "Drive property values cannot be None."
            raise InvalidDrivePropertyError(msg)
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @staticmethod
    def validate_drive_properties(
        properties: dict[str, str],
        *,
        kind: str,
    ) -> None:
        """Enforce Drive custom property count and byte limits."""
        if len(properties) > MAX_DRIVE_PROPERTIES_PER_FILE:
            msg = (
                f"Drive {kind} exceed limit of {MAX_DRIVE_PROPERTIES_PER_FILE} "
                f"keys per file (got {len(properties)})."
            )
            raise InvalidDrivePropertyError(msg)
        for key, value in properties.items():
            key_bytes = len(key.encode("utf-8"))
            value_bytes = len(value.encode("utf-8"))
            if key_bytes > MAX_DRIVE_PROPERTY_BYTES:
                msg = (
                    f"Drive {kind} key {key!r} exceeds "
                    f"{MAX_DRIVE_PROPERTY_BYTES} UTF-8 bytes."
                )
                raise InvalidDrivePropertyError(msg)
            if value_bytes > MAX_DRIVE_PROPERTY_BYTES:
                msg = (
                    f"Drive {kind} value for {key!r} exceeds "
                    f"{MAX_DRIVE_PROPERTY_BYTES} UTF-8 bytes."
                )
                raise InvalidDrivePropertyError(msg)

    @staticmethod
    def merge_property_patch(
        existing: dict[str, str],
        patch: dict[str, str],
    ) -> dict[str, str]:
        """Merge a property patch while preserving unrelated existing keys."""
        merged = dict(existing)
        merged.update(patch)
        return merged

    def build_write_payloads(
        self,
        model: type[GDMModel],
        instance: GDMModel,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, str]]:
        drive_body: dict[str, Any] = {}
        metadata_values: dict[str, Any] = {}
        properties: dict[str, str] = {}
        app_properties: dict[str, str] = {}
        inventory_item = instance._gdm_state.inventory_item
        for name, info in model.model_fields.items():
            if get_gdm_field_metadata(info).primary_key:
                continue
            target_kind, target_name = field_storage_target(
                name, info, storage=model.gdm_storage()
            )
            value = getattr(instance, name)
            if target_kind == "drive":
                if target_name in READ_ONLY_DRIVE_FIELDS:
                    if inventory_item is not None:
                        current = self._drive_field_value(inventory_item, target_name)
                        if value != current:
                            msg = f"Drive field {target_name!r} is read-only."
                            raise UnsupportedOperationError(msg)
                    continue
                if inventory_item is not None:
                    current = self._drive_field_value(inventory_item, target_name)
                    if value == current:
                        continue
                drive_body[target_name] = value
            elif target_kind == "metadata":
                metadata_values[target_name] = value
            elif target_kind == "property":
                if value is not None:
                    properties[target_name] = self.serialize_property_value(value)
            elif target_kind == "app_property":
                if value is not None:
                    app_properties[target_name] = self.serialize_property_value(value)
            elif target_kind == "source":
                metadata_values[target_name] = value
        unknown = dict(instance._gdm_state.unknown_fields)
        metadata_values.update(unknown)
        return drive_body, metadata_values, properties, app_properties

    def routing_app_properties(self, model: type[GDMModel]) -> dict[str, str]:
        props = {
            "gdm_project": self.project_id,
            "gdm_model": model.gdm_model_name(),
        }
        if model.__gdm_schema_version__:
            props["gdm_schema"] = model.__gdm_schema_version__
        return props

    def assert_resource_belongs_to_project(
        self,
        item: DriveInventoryItem,
    ) -> None:
        """Reject a typed object tagged for another project unless adopting."""
        if self.adoption_mode:
            return
        tagged = item.app_properties.get("gdm_project")
        if tagged and tagged != self.project_id:
            msg = (
                f"Resource {item.file_id} belongs to project {tagged}, "
                f"not {self.project_id}."
            )
            raise ForeignProjectError(msg)

    def create_native(
        self,
        *,
        folder_id: str,
        name: str,
        kind: str,
        model: type[GDMModel] | None = None,
        metadata_values: dict[str, Any] | None = None,
    ) -> DriveInventoryItem:
        mime = NATIVE_CREATE_MIMES.get(kind.lower())
        if mime is None:
            msg = f"Unsupported native kind {kind!r}."
            raise UnsupportedOperationError(msg)
        if model is not None and metadata_values is not None:
            self.validate_create_metadata(model, metadata_values)
        body: dict[str, Any] = {"name": name, "mimeType": mime, "parents": [folder_id]}
        if model is not None:
            routing = self.routing_app_properties(model)
            body["appProperties"] = routing

        def _create() -> dict[str, Any]:
            return self.drive.service.files.create(
                body=body,
                fields=self.DEFAULT_FIELDS,
                supportsAllDrives=self.drive.supports_all_drives,
            )

        item = inventory_item_from_file(self.drive._call(_create, retryable=False))
        return item

    def write_metadata_only(
        self,
        *,
        model: type[GDMModel],
        file_id: str,
        metadata_values: dict[str, Any],
        metadata_store: CollectionMetadataStore,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> CollectionMetadataRow:
        """Persist collection metadata without touching native file media."""
        return metadata_store.upsert_row(
            file_id,
            metadata_values,
            expected_version=expected_version,
            expected_fingerprint=expected_fingerprint,
        )

    def upload_binary(
        self,
        *,
        folder_id: str,
        name: str,
        data: bytes,
        mime_type: str,
        model: type[GDMModel] | None = None,
    ) -> DriveInventoryItem:
        import io

        from googleapiclient.http import MediaIoBaseUpload

        media = MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype=mime_type,
            resumable=False,
        )
        body: dict[str, Any] = {
            "name": name,
            "mimeType": mime_type,
            "parents": [folder_id],
        }
        if model is not None:
            body["appProperties"] = self.routing_app_properties(model)

        def _create() -> dict[str, Any]:
            return self.drive.service.files.create(
                body=body,
                media_body=media,
                fields=self.DEFAULT_FIELDS,
                supportsAllDrives=self.drive.supports_all_drives,
            )

        return inventory_item_from_file(self.drive._call(_create, retryable=False))

    def adopt(
        self,
        *,
        folder_id: str,
        file_id: str,
        move: bool = False,
        shortcut: bool = False,
    ) -> DriveInventoryItem:
        if self._resource_scope_validator is not None:
            destination = self._resource_scope_validator(folder_id)
            if str(destination.get("mimeType", "")) != FOLDER_MIME:
                msg = f"Destination {folder_id!r} is not a Drive folder."
                raise UnsupportedOperationError(msg)
        if move and shortcut:
            msg = "Specify at most one of move=True or shortcut=True."
            raise UnsupportedOperationError(msg)
        meta = self.drive.get_metadata(file_id)
        tagged = meta.app_properties.get("gdm_project")
        if not self.adoption_mode and tagged and tagged != self.project_id:
            msg = (
                f"Resource {file_id} belongs to project {tagged}, "
                f"not {self.project_id}."
            )
            raise ForeignProjectError(msg)
        if folder_id in meta.parents:
            return inventory_item_from_file(
                {
                    "id": meta.file_id,
                    "name": meta.name,
                    "mimeType": meta.mime_type,
                    "version": meta.version,
                    "md5Checksum": meta.md5_checksum,
                    "parents": meta.parents,
                    "appProperties": meta.app_properties,
                    "properties": getattr(meta, "properties", {}),
                }
            )
        if shortcut:
            body = {
                "name": meta.name,
                "mimeType": SHORTCUT_MIME,
                "parents": [folder_id],
                "shortcutDetails": {
                    "targetId": meta.file_id,
                    "targetMimeType": meta.mime_type,
                },
            }

            def _create_shortcut() -> dict[str, Any]:
                return self.drive.service.files.create(
                    body=body,
                    fields=self.DEFAULT_FIELDS,
                    supportsAllDrives=self.drive.supports_all_drives,
                )

            return inventory_item_from_file(
                self.drive._call(_create_shortcut, retryable=False)
            )
        if not move:
            msg = (
                f"File {file_id} is outside folder {folder_id}; "
                "pass move=True or shortcut=True."
            )
            raise UnsupportedOperationError(msg)
        add = folder_id
        remove = ",".join(meta.parents)

        def _move() -> dict[str, Any]:
            return self.drive.service.files.update(
                fileId=file_id,
                addParents=add,
                removeParents=remove,
                fields=self.DEFAULT_FIELDS,
                supportsAllDrives=self.drive.supports_all_drives,
            )

        return inventory_item_from_file(self.drive._call(_move, retryable=False))

    def detach(
        self,
        *,
        file_id: str,
        collection_folder_id: str,
        to_folder_id: str,
    ) -> DriveInventoryItem:
        if self._resource_scope_validator is not None:
            destination = self._resource_scope_validator(to_folder_id)
            if str(destination.get("mimeType", "")) != FOLDER_MIME:
                msg = f"Destination {to_folder_id!r} is not a Drive folder."
                raise UnsupportedOperationError(msg)

        def _update() -> dict[str, Any]:
            return self.drive.service.files.update(
                fileId=file_id,
                addParents=to_folder_id,
                removeParents=collection_folder_id,
                fields=self.DEFAULT_FIELDS,
                supportsAllDrives=self.drive.supports_all_drives,
            )

        return inventory_item_from_file(self.drive._call(_update, retryable=False))

    def trash(self, file_id: str) -> None:
        def _trash() -> dict[str, Any]:
            return self.drive.service.files.update(
                fileId=file_id,
                body={"trashed": True},
                fields="id,trashed",
                supportsAllDrives=self.drive.supports_all_drives,
            )

        self.drive._call(_trash, retryable=False)

    def delete_permanently(self, file_id: str) -> None:
        self.drive.delete(file_id)

    def content_handle(
        self,
        item: DriveInventoryItem,
        *,
        on_item_updated: Callable[[DriveInventoryItem], None] | None = None,
    ) -> ContentHandle:
        self.assert_resource_belongs_to_project(item)
        return ContentHandle(
            item=item,
            drive_files=self.drive.service.files,
            call=self.drive._call,
            documents_service=self._documents,
            slides_service=self._slides,
            sheets_service=self._sheets,
            on_item_updated=on_item_updated,
        )

    def matches_model(
        self,
        model: type[GDMModel],
        item: DriveInventoryItem,
    ) -> bool:
        patterns = model.__gdm_mime_patterns__  # type: ignore[attr-defined]
        if not patterns:
            return True
        return any(mime_matches(item.mime_type, pattern) for pattern in patterns)

    @staticmethod
    def _drive_field_value(item: DriveInventoryItem, field_name: str) -> Any:
        mapping = {
            "id": item.file_id,
            "name": item.name,
            "mimeType": item.mime_type,
            "description": item.description,
            "starred": item.starred,
            "parents": list(item.parents),
            "createdTime": item.created_time,
            "modifiedTime": item.modified_time,
            "webViewLink": item.web_view_link,
            "size": item.size,
            "md5Checksum": item.md5_checksum,
            "capabilities": item.capabilities,
            "shortcutDetails": {
                "targetId": item.shortcut_target_id,
                "targetMimeType": item.shortcut_target_mime,
            }
            if item.is_shortcut
            else None,
            "resourceKey": item.resource_key,
        }
        if field_name not in mapping:
            msg = f"Unsupported drive field {field_name!r}."
            raise UnsupportedOperationError(msg)
        return mapping[field_name]


def collection_record_key(item: DriveInventoryItem) -> str:
    return item.file_id


def collection_drive_fingerprint(item: DriveInventoryItem) -> str:
    return drive_fingerprint(item)
