"""GDM model base class and binding state."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr
from pydantic._internal._model_construction import ModelMetaclass

from gdm.errors import (
    InvalidForeignKeyError,
    InvalidModelConfigError,
    UnknownSourceFieldError,
)
from gdm.fields import Field, field_storage_target, get_gdm_field_metadata, source_name
from gdm.query import FieldRef

MappingMode = Literal["partial", "permissive", "strict"]
StorageMode = Literal["file", "sheet", "collection"]
CodecMode = Literal["json", "text", "bytes", "yaml"]

VALID_CODECS: frozenset[str] = frozenset({"json", "text", "bytes", "yaml"})

SYSTEM_ID_FIELD = "_gdm_id"
SYSTEM_VERSION_FIELD = "_gdm_version"
SYSTEM_UPDATED_AT_FIELD = "_gdm_updated_at"


class GDMModelState:
    """Private binding state for a persisted model instance."""

    __slots__ = (
        "collection_backend",
        "content_handle",
        "drive_fingerprint",
        "drive_version",
        "inventory_item",
        "locator",
        "metadata_fingerprint",
        "metadata_store",
        "metadata_version",
        "persisted",
        "source_fingerprint",
        "source_version",
        "unknown_fields",
    )

    def __init__(self) -> None:
        self.locator: Any | None = None
        self.source_version: str | None = None
        self.source_fingerprint: str | None = None
        self.unknown_fields: dict[str, Any] = {}
        self.persisted: bool = False
        self.drive_version: str | None = None
        self.drive_fingerprint: str | None = None
        self.metadata_version: str | None = None
        self.metadata_fingerprint: str | None = None
        self.metadata_store: Any | None = None
        self.inventory_item: Any | None = None
        self.collection_backend: Any | None = None
        self.content_handle: Any | None = None


class GDMModelMetaclass(ModelMetaclass):
    """Metaclass enabling class-level field expressions."""

    # Depth counter for in-flight class construction. Pydantic reads field
    # defaults with ``getattr(cls, field_name)`` while collecting fields, so
    # returning a FieldRef during construction would overwrite the real
    # FieldInfo contributed by a plain mixin such as DriveObjectMixin.
    _gdm_building: ClassVar[int] = 0

    def __new__(
        mcls,
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> type:
        GDMModelMetaclass._gdm_building += 1
        try:
            return super().__new__(mcls, name, bases, namespace, **kwargs)
        finally:
            GDMModelMetaclass._gdm_building -= 1

    def __getattribute__(cls, name: str) -> Any:
        if not name.startswith("_") and GDMModelMetaclass._gdm_building == 0:
            try:
                fields = super().__getattribute__("model_fields")
            except AttributeError:
                fields = {}
            if name in fields:
                return FieldRef(cls, name)
        return super().__getattribute__(name)


class DriveObjectMixin:
    """Optional read-only Drive identity and metadata fields."""

    drive_id: str | None = Field(default=None, drive="id")
    name: str | None = Field(default=None, drive="name")
    mime_type: str | None = Field(default=None, drive="mimeType")
    description: str | None = Field(default=None, drive="description")
    starred: bool | None = Field(default=None, drive="starred")
    web_view_link: str | None = Field(default=None, drive="webViewLink")
    created_time: str | None = Field(default=None, drive="createdTime")
    modified_time: str | None = Field(default=None, drive="modifiedTime")


class GDMModel(BaseModel, metaclass=GDMModelMetaclass):
    """Pydantic model with GDM persistence metadata."""

    model_config = ConfigDict(
        validate_assignment=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    __gdm_storage__: ClassVar[StorageMode | None] = None
    __gdm_source__: ClassVar[str | None] = None
    __gdm_collection__: ClassVar[str | None] = None
    __gdm_mime_patterns__: ClassVar[tuple[str, ...]] = ()
    __gdm_mapping__: ClassVar[MappingMode] = "partial"
    __gdm_schema_version__: ClassVar[str | None] = None
    __gdm_model_name__: ClassVar[str | None] = None
    __gdm_codec__: ClassVar[CodecMode | None] = None

    _gdm_state: GDMModelState = PrivateAttr(default_factory=GDMModelState)

    def __init_subclass__(
        cls,
        *,
        storage: StorageMode | None = None,
        source: str | None = None,
        collection: str | None = None,
        mime: str | tuple[str, ...] | None = None,
        mapping: MappingMode | None = None,
        schema_version: str | int | None = None,
        codec: CodecMode | None = None,
        table: bool = False,
        __gdm_model_name__: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        # Storage shape (storage/codec/mapping) is inherited when not restated, so
        # reusable bases such as DriveObject work without repeating keywords.
        # Resource identity (source/collection/schema_version) stays per-model.
        redeclares_storage = storage is not None or table
        resolved_codec = codec
        if resolved_codec is None and not redeclares_storage:
            resolved_codec = cls.__gdm_codec__
        resolved_storage = storage if storage is not None else cls.__gdm_storage__
        if resolved_storage == "file":
            resolved_storage = "collection"
        if table:
            if storage is not None and storage != "sheet":
                msg = "table=True is an alias for storage='sheet' and cannot conflict."
                raise InvalidModelConfigError(msg)
            resolved_storage = "sheet"
        if resolved_codec is not None and resolved_codec not in VALID_CODECS:
            msg = (
                f"Unsupported codec {resolved_codec!r}; "
                f"expected one of {sorted(VALID_CODECS)}."
            )
            raise InvalidModelConfigError(msg)
        if resolved_codec is not None:
            if resolved_storage not in {None, "collection", "file"}:
                msg = "codec= is only supported for storage='file' models."
                raise InvalidModelConfigError(msg)
            resolved_storage = "file"
        cls.__gdm_storage__ = resolved_storage
        cls.__gdm_source__ = source
        cls.__gdm_collection__ = collection
        if mime is None:
            pass  # inherit the base model's patterns, or the class namespace value
        elif isinstance(mime, str):
            cls.__gdm_mime_patterns__ = (mime,)
        else:
            cls.__gdm_mime_patterns__ = tuple(mime)
        cls.__gdm_mapping__ = mapping if mapping is not None else cls.__gdm_mapping__
        cls.__gdm_schema_version__ = (
            None if schema_version is None else str(schema_version)
        )
        cls.__gdm_model_name__ = __gdm_model_name__ or cls.__name__
        cls.__gdm_codec__ = resolved_codec

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        cls._validate_foreign_keys()
        cls._validate_field_targets()
        cls._validate_codec_config()

    @classmethod
    def _validate_codec_config(cls) -> None:
        if not cls.uses_legacy_file_codec():
            return
        from gdm.codecs import validate_codec_model

        validate_codec_model(cls)

    @classmethod
    def _validate_foreign_keys(cls) -> None:
        for name, info in cls.model_fields.items():
            metadata = get_gdm_field_metadata(info)
            if metadata.foreign_key is None:
                continue
            target = metadata.foreign_key
            if "." not in target:
                msg = (
                    f"foreign_key on {cls.__name__}.{name} must use "
                    f"'Model.field' format, got {target!r}."
                )
                raise InvalidForeignKeyError(msg)
            model_name, field_name = target.split(".", 1)
            if not model_name or not field_name:
                msg = f"Invalid foreign_key {target!r} on {cls.__name__}.{name}."
                raise InvalidForeignKeyError(msg)

    @classmethod
    def _validate_field_targets(cls) -> None:
        storage = cls.__gdm_storage__
        for name, info in cls.model_fields.items():
            metadata = get_gdm_field_metadata(info)
            if metadata.source is not None and storage == "collection":
                if metadata.drive or metadata.metadata:
                    continue
                if cls.uses_legacy_file_codec():
                    continue
            targets = sum(
                1
                for value in (
                    metadata.source,
                    metadata.drive,
                    metadata.metadata,
                    metadata.property,
                    metadata.app_property,
                )
                if value is not None
            )
            if targets > 1:
                msg = f"Field {cls.__name__}.{name} declares multiple storage targets."
                raise InvalidModelConfigError(msg)

    @classmethod
    def gdm_model_name(cls) -> str:
        """Return the stable storage name for this model."""
        return cls.__gdm_model_name__ or cls.__name__

    @classmethod
    def gdm_storage(cls) -> StorageMode | None:
        """Return configured storage kind, if any."""
        return cls.__gdm_storage__

    @classmethod
    def gdm_collection_path(cls) -> str | None:
        """Return configured collection path hint, if any."""
        return cls.__gdm_collection__

    @classmethod
    def gdm_mapping(cls) -> MappingMode:
        """Return configured mapping mode."""
        return cls.__gdm_mapping__

    @classmethod
    def gdm_codec(cls) -> CodecMode | None:
        """Return configured file codec, if any."""
        return cls.__gdm_codec__

    @classmethod
    def uses_legacy_file_codec(cls) -> bool:
        """Return whether this model uses opt-in structured file codec storage."""
        return cls.__gdm_storage__ == "file" and cls.__gdm_codec__ is not None

    @classmethod
    def is_collection_model(cls) -> bool:
        """Return whether this model maps a Drive folder collection."""
        return cls.__gdm_storage__ == "collection"

    @classmethod
    def primary_key_field(cls) -> str | None:
        """Return the Python field name marked as primary key, if any."""
        for name, info in cls.model_fields.items():
            if get_gdm_field_metadata(info).primary_key:
                return name
        pk_drive = None
        for name, info in cls.model_fields.items():
            metadata = get_gdm_field_metadata(info)
            if metadata.drive == "id":
                pk_drive = name
        return pk_drive

    @classmethod
    def field_source_map(cls) -> dict[str, str]:
        """Map Python field names to source-side names."""
        storage = cls.gdm_storage()
        return {
            name: source_name(name, info)
            for name, info in cls.model_fields.items()
            if field_storage_target(name, info, storage=storage)[0] != "drive"
        }

    @classmethod
    def source_field_map(cls) -> dict[str, str]:
        """Map source-side names to Python field names."""
        return {source: name for name, source in cls.field_source_map().items()}

    @property
    def gdm_extra(self) -> Mapping[str, Any]:
        """Unknown source fields preserved in permissive mapping mode."""
        if self.__class__.gdm_mapping() != "permissive":
            return {}
        return dict(self._gdm_state.unknown_fields)

    @property
    def gdm_locator(self) -> Any | None:
        """Return the bound record locator, if any."""
        return self._gdm_state.locator

    @property
    def gdm_is_persisted(self) -> bool:
        """Return whether the instance is bound to stored data."""
        return self._gdm_state.persisted

    @property
    def content(self) -> Any:
        """Lazy native content handle for collection objects."""
        state = self._gdm_state
        if state.content_handle is None:
            if state.inventory_item is None:
                msg = "Content handle requires a materialized collection object."
                raise InvalidModelConfigError(msg)
            from gdm.collection.backend import CollectionBackend

            backend = state.collection_backend
            if backend is None or not isinstance(backend, CollectionBackend):
                msg = "Collection backend is not attached to this instance."
                raise InvalidModelConfigError(msg)

            def _update_item(item: Any) -> None:
                from gdm.collection.backend import collection_drive_fingerprint

                fingerprint = collection_drive_fingerprint(item)
                state.inventory_item = item
                state.drive_version = item.version
                state.drive_fingerprint = fingerprint
                state.source_version = item.version
                state.source_fingerprint = fingerprint

            state.content_handle = backend.content_handle(
                state.inventory_item,
                on_item_updated=_update_item,
            )
        return state.content_handle

    def gdm_bind(
        self,
        *,
        locator: Any,
        version: str | None,
        fingerprint: str | None,
        unknown_fields: dict[str, Any] | None = None,
        persisted: bool = True,
    ) -> None:
        """Attach private binding state after a read or successful write."""
        state = self._gdm_state
        state.locator = locator
        state.source_version = version
        state.source_fingerprint = fingerprint
        state.unknown_fields = dict(unknown_fields or {})
        state.persisted = persisted

    def gdm_bind_collection(
        self,
        *,
        locator: Any,
        drive_version: str | None,
        drive_fingerprint: str | None,
        metadata_version: str | None,
        metadata_fingerprint: str | None,
        unknown_fields: dict[str, Any] | None = None,
        metadata_store: Any | None = None,
        inventory_item: Any | None = None,
        collection_backend: Any | None = None,
        persisted: bool = True,
    ) -> None:
        """Attach collection-specific binding state."""
        state = self._gdm_state
        state.locator = locator
        state.drive_version = drive_version
        state.drive_fingerprint = drive_fingerprint
        state.metadata_version = metadata_version
        state.metadata_fingerprint = metadata_fingerprint
        state.source_version = drive_version
        state.source_fingerprint = drive_fingerprint
        state.unknown_fields = dict(unknown_fields or {})
        state.metadata_store = metadata_store
        state.inventory_item = inventory_item
        state.collection_backend = collection_backend
        state.content_handle = None
        state.persisted = persisted

    def gdm_clear_binding(self) -> None:
        """Clear binding state during rollback."""
        self._gdm_state = GDMModelState()

    @classmethod
    def gdm_from_source(
        cls,
        values: Mapping[str, Any],
        *,
        unknown: Mapping[str, Any] | None = None,
    ) -> GDMModel:
        """Construct an instance from source-side field names."""
        unknown_values = dict(unknown or {})
        mapping_mode = cls.gdm_mapping()
        source_to_field = cls.source_field_map()
        mapped: dict[str, Any] = {}
        system_fields = {SYSTEM_ID_FIELD, SYSTEM_VERSION_FIELD, SYSTEM_UPDATED_AT_FIELD}

        for source_key, raw in values.items():
            if source_key in source_to_field:
                mapped[source_to_field[source_key]] = raw
            elif source_key in cls.model_fields:
                mapped[source_key] = raw
            elif source_key in system_fields:
                continue
            elif mapping_mode == "strict":
                raise UnknownSourceFieldError(
                    f"Unknown source field {source_key!r} for {cls.__name__}",
                    fields=(source_key,),
                )
            else:
                unknown_values[source_key] = raw

        instance = cls.model_validate(mapped)
        if mapping_mode in {"partial", "permissive"}:
            instance._gdm_state.unknown_fields = dict(unknown_values)
        return instance

    def gdm_to_source(self, *, include_unknown: bool = True) -> dict[str, Any]:
        """Serialize mapped fields to source-side names."""
        payload: dict[str, Any] = {}
        storage = self.__class__.gdm_storage()
        for name, info in self.__class__.model_fields.items():
            target_kind, target_name = field_storage_target(name, info, storage=storage)
            if target_kind == "drive":
                continue
            payload[target_name] = getattr(self, name)
        if include_unknown:
            payload.update(self._gdm_state.unknown_fields)
        return payload

    def gdm_assign_primary_key(self) -> str:
        """Ensure a stable primary key exists, generating a UUID when needed."""
        if self.__class__.is_collection_model():
            state = self._gdm_state
            if state.locator is not None:
                return state.locator.record_key
            pk_field = self.__class__.primary_key_field()
            if pk_field is not None:
                value = getattr(self, pk_field)
                if value is not None:
                    return str(value)
        pk_field = self.__class__.primary_key_field()
        if pk_field is None:
            state = self._gdm_state
            existing = state.unknown_fields.get(SYSTEM_ID_FIELD)
            if existing is not None:
                return str(existing)
            generated = str(uuid.uuid4())
            state.unknown_fields[SYSTEM_ID_FIELD] = generated
            return generated
        value = getattr(self, pk_field)
        if value is None:
            generated = str(uuid.uuid4())
            object.__setattr__(self, pk_field, generated)
            return generated
        return str(value)

    def gdm_record_key(self) -> str:
        """Return the logical primary key for this instance."""
        state = self._gdm_state
        if state.locator is not None and state.persisted:
            return state.locator.record_key
        return self.gdm_assign_primary_key()


class DriveObject(GDMModel, DriveObjectMixin, storage="collection"):
    """Base model for a Drive folder collection of native files.

    Combines :class:`GDMModel`, :class:`DriveObjectMixin`, and
    ``storage="collection"`` so the common case needs no boilerplate::

        class Asset(DriveObject, collection="Assets"):
            pass

    Subclasses inherit ``storage="collection"`` and may add ``collection=``,
    ``mime=``, and application ``Field(metadata=...)`` columns.
    """


def utc_now_iso() -> str:
    """Return a UTC timestamp string for sheet metadata."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()
