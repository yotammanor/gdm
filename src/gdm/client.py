"""High-level GDM client for connecting to Drive-backed projects."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from google.auth.credentials import Credentials

from gdm.auth import (
    build_credentials,
    build_docs_service,
    build_drive_service,
    build_explorer_service,
    build_sheets_service,
    build_slides_service,
    scopes_for_discovery,
)
from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.collection.backend import CollectionBackend
from gdm.collection.constants import FOLDER_MIME, SHEET_MIME
from gdm.errors import GDMError
from gdm.google_api import (
    DriveExplorerAdapter,
    ExecutingDriveService,
)
from gdm.indexes import IndexStore, StalePolicy
from gdm.permissions import PermissionsManager
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.session import Session
from gdm.sync import ChangesPoller


class GDMClient:
    """Recommended entry point for connecting to a root-scoped GDM project."""

    def __init__(
        self,
        *,
        project: Project,
        drive: DriveBackend,
        sheets: SheetsBackend,
        registry_backend: RegistryBackend,
        drive_service: ExecutingDriveService,
        credentials: Credentials | None = None,
        explorer: Any | None = None,
    ) -> None:
        self.project = project
        self.drive = drive
        self.sheets = sheets
        self._backend = registry_backend
        self.drive_service = drive_service
        self.credentials = credentials
        self._explorer = explorer

    @classmethod
    def connect(
        cls,
        *,
        root_folder_id: str,
        credentials: Credentials | None = None,
        create: bool = False,
        adoption_mode: bool = False,
        scan_limit: int = 1000,
        supports_all_drives: bool = True,
        shared_drive_id: str | None = None,
        use_adc: bool = False,
        token_path: str | None = None,
        service_account_file: str | None = None,
        scopes: Sequence[str] | None = None,
        indexes_dir: str | None = None,
        stale_policy: StalePolicy = StalePolicy.FALLBACK,
        metadata_backend: str = "sheets",
    ) -> GDMClient:
        """Open or create a project anchored to ``root_folder_id``.

        The default scopes include full Drive access because the primary GDM
        workflow adopts existing folders and files. Pass an explicit narrower
        scope sequence when all resources are created or selected by your app.
        """
        creds = build_credentials(
            credentials=credentials,
            use_adc=use_adc,
            token_path=token_path,
            service_account_file=service_account_file,
            scopes=list(scopes) if scopes is not None else scopes_for_discovery(),
        )
        drive_service = build_drive_service(
            creds,
            wrap=True,
            supports_all_drives=supports_all_drives,
        )
        sheets_service = build_sheets_service(creds, wrap=True)
        docs_service = build_docs_service(creds, wrap=True)
        slides_service = build_slides_service(creds, wrap=True)
        if create:
            project = Project.create_new(
                root_folder_id=root_folder_id,
                drive=drive_service,
                supports_all_drives=supports_all_drives,
            )
        else:
            project = Project.open(
                root_folder_id=root_folder_id,
                drive=drive_service,
                supports_all_drives=supports_all_drives,
                adoption_mode=adoption_mode,
            )
        drive = DriveBackend(
            drive_service,
            project_id=str(project.project_id),
            supports_all_drives=supports_all_drives,
            shared_drive_id=shared_drive_id,
        )
        sheets = SheetsBackend(sheets_service)
        index_store = IndexStore(indexes_dir) if indexes_dir else None
        backend = RegistryBackend(
            project=project,
            drive=drive,
            sheets=sheets,
            scan_limit=scan_limit,
            collection_backend=CollectionBackend(
                drive,
                project_id=str(project.project_id),
                documents_service=docs_service,
                slides_service=slides_service,
                sheets_service=sheets_service,
                adoption_mode=project.adoption_mode,
                resource_scope_validator=project.assert_resource_in_project_scope,
            ),
            index_store=index_store,
            stale_policy=stale_policy,
            default_metadata_backend=metadata_backend,
        )
        explorer_adapter = DriveExplorerAdapter(drive_service)
        from gdm.explore import Explorer

        explorer = Explorer(explorer_adapter)
        return cls(
            project=project,
            drive=drive,
            sheets=sheets,
            registry_backend=backend,
            drive_service=drive_service,
            credentials=creds,
            explorer=explorer,
        )

    @classmethod
    def open(cls, **kwargs: Any) -> GDMClient:
        """Open an existing project (alias for ``connect(create=False)``)."""
        return cls.connect(create=False, **kwargs)

    @classmethod
    def create(cls, **kwargs: Any) -> GDMClient:
        """Create a new project (alias for ``connect(create=True)``)."""
        return cls.connect(create=True, **kwargs)

    def bind(
        self,
        model: type[Any],
        *,
        resource_id: str,
        kind: ResourceKind,
        parent_id: str | None = None,
        worksheet: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RegistryEntry:
        """Register a model binding in the project registry."""
        metadata = self.project.assert_resource_in_project_scope(resource_id)
        mime_type = str(metadata.get("mimeType", ""))
        if kind is ResourceKind.FOLDER and mime_type != FOLDER_MIME:
            msg = (
                f"Resource {resource_id!r} is not a Drive folder "
                f"(mimeType={mime_type!r})."
            )
            raise GDMError(msg)
        if (
            kind in {ResourceKind.SPREADSHEET, ResourceKind.WORKSHEET}
            and mime_type != SHEET_MIME
        ):
            msg = (
                f"Resource {resource_id!r} is not a Google Sheet "
                f"(mimeType={mime_type!r})."
            )
            raise GDMError(msg)
        if parent_id is not None:
            parent = self.project.assert_resource_in_project_scope(parent_id)
            parent_mime = str(parent.get("mimeType", ""))
            if parent_mime != FOLDER_MIME:
                msg = (
                    f"Parent resource {parent_id!r} is not a Drive folder "
                    f"(mimeType={parent_mime!r})."
                )
                raise GDMError(msg)
        payload = dict(extra or {})
        if worksheet is not None:
            payload["worksheet"] = worksheet
        entry = RegistryEntry(
            model_name=model.gdm_model_name(),
            resource_id=resource_id,
            kind=kind,
            parent_id=parent_id,
            extra=payload,
        )
        self._backend.bind_model(model, entry)
        return entry

    def bind_collection(
        self,
        model: type[Any],
        *,
        folder_id: str | None = None,
        path: str | None = None,
    ) -> RegistryEntry:
        """Bind a collection model to a Drive folder by ID or discovery path.

        Exactly one of ``folder_id`` or ``path`` must be supplied unless the
        model declares ``collection=`` and neither is given (uses the hint).

        Persists the immutable folder ID in the project registry. Path hints are
        stored for documentation only; subsequent reads and writes use the ID.
        Mutates the remote registry on success.
        """
        return self._backend.bind_collection(
            model,
            folder_id=folder_id,
            path=path,
        )

    def collection(
        self,
        model: type[Any],
        *,
        folder_id: str | None = None,
        metadata_store: Any | None = None,
    ) -> Any:
        """Return a typed collection for ``model``.

        When ``folder_id`` is omitted and the model is not yet bound, auto-binds
        using the model's ``collection=`` path hint if present. Auto-binding
        mutates the remote registry.
        """
        from gdm.collection.collection import DriveCollection
        from gdm.errors import NotFoundError

        if folder_id is None:
            try:
                folder_id = self._backend._collection_folder_id(model)
            except NotFoundError:
                if model.gdm_collection_path() is not None:
                    self.bind_collection(model)
                    folder_id = self._backend._collection_folder_id(model)
                else:
                    raise
        metadata = self.project.assert_resource_in_project_scope(folder_id)
        mime_type = str(metadata.get("mimeType", ""))
        if mime_type != FOLDER_MIME:
            msg = (
                f"Resource {folder_id!r} is not a Drive folder "
                f"(mimeType={mime_type!r})."
            )
            raise GDMError(msg)
        store = metadata_store
        if store is None:
            store = self._backend._metadata_store_for(model)
        return DriveCollection(
            model=model,
            folder_id=folder_id,
            backend=self._backend._collection,
            metadata_store=store,
            scan_limit=self._backend.scan_limit,
        )

    def session(
        self,
        *,
        read_only: bool = False,
        scan_limit: int | None = None,
        stale_policy: StalePolicy | None = None,
    ) -> Session:
        """Return a session bound to this project's registry backend."""
        if scan_limit is not None:
            self._backend.scan_limit = scan_limit
        if stale_policy is not None and self._backend.index_runtime is not None:
            self._backend.index_runtime.stale_policy = stale_policy
        return Session(
            self._backend,
            read_only=read_only,
            scan_limit=scan_limit,
            index_runtime=self._backend.index_runtime,
        )

    def configure_indexes(
        self,
        indexes_dir: str,
        *,
        stale_policy: StalePolicy = StalePolicy.FALLBACK,
    ) -> IndexStore:
        """Attach a filesystem-backed index store to this client."""
        store = IndexStore(indexes_dir)
        self._backend.configure_index_store(store, stale_policy=stale_policy)
        return store

    def rebuild_index(self, model: type[Any], field_name: str) -> int:
        """Rebuild one model field index from the live backend."""
        return self._backend.rebuild_index(model, field_name)

    def rebuild_model_indexes(self, model: type[Any]) -> dict[str, int]:
        """Rebuild all indexed fields for ``model``."""
        return self._backend.rebuild_model_indexes(model)

    def explore(
        self,
        root_folder_id: str | None = None,
        *,
        max_resources: int | None = None,
    ) -> Any:
        """Inventory a Drive root without mutating remote resources."""
        if self._explorer is None:
            from gdm.explore import Explorer

            adapter = build_explorer_service(credentials=self.credentials)
            self._explorer = Explorer(adapter, max_resources=max_resources)
        elif max_resources is not None:
            from gdm.explore import Explorer

            self._explorer = Explorer(
                self._explorer.service,
                page_size=self._explorer.page_size,
                supports_all_drives=self._explorer.supports_all_drives,
                include_items_from_all_drives=(
                    self._explorer.include_items_from_all_drives
                ),
                max_resources=max_resources,
            )
        root = root_folder_id or self.project.root_folder_id
        return self._explorer.explore(root)

    @property
    def permissions(self) -> PermissionsManager:
        """Drive ACL inspection and sharing helpers."""
        return PermissionsManager(self.drive_service)

    @property
    def changes(self) -> ChangesPoller:
        """Drive Changes API polling primitives."""
        return ChangesPoller(self.drive_service)

    @property
    def backend(self) -> RegistryBackend:
        """Low-level registry backend implementing the Backend protocol."""
        return self._backend

    def migration_target(
        self,
        *,
        bindings: dict[str, str] | None = None,
        indexes_dir: str | None = None,
        checkpoint_dir: str | None = None,
        schema_field_lookup: Any | None = None,
        schema_field_target_lookup: Any | None = None,
    ) -> Any:
        """Return a production migration target backed by this client."""
        from pathlib import Path

        from gdm.migration_target import build_production_migration_target

        store_path = indexes_dir or ".gdm/indexes"
        cp_dir = Path(checkpoint_dir) if checkpoint_dir else None
        return build_production_migration_target(
            self,
            bindings=bindings,
            indexes_dir=store_path,
            checkpoint_dir=cp_dir,
            schema_field_lookup=schema_field_lookup,
            schema_field_target_lookup=schema_field_target_lookup,
        )


GDM = GDMClient
