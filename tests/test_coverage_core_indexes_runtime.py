"""Coverage for runtime index integration on registry, client, and query."""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from gdm.backends.drive import DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.client import GDMClient
from gdm.errors import GDMError, ScanLimitExceeded
from gdm.fields import Field
from gdm.indexes import GdmIndexError, IndexStore, StalePolicy, decode_index_key
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.query import extract_index_lookup, select
from gdm.session import Session
from tests.fakes import FakeDrive, FakeSheetsService, MemoryDriveService


class IndexedTask(GDMModel, storage="sheet", source="tasks", mapping="partial"):
    id: str = Field(primary_key=True, source="task_id")
    email: str = Field(index=True)
    title: str


class IndexedAsset(GDMModel, storage="collection", mapping="partial"):
    id: str = Field(primary_key=True, drive="id")
    tag: str = Field(metadata="tag", index=True)


def _registry_with_indexes(
    tmp_path,
    *,
    table: list[list[str]] | None = None,
) -> tuple[RegistryBackend, Project]:
    drive_wrapper = FakeDrive()
    root_id = "root-folder"
    drive_wrapper.files.files[root_id] = {
        "id": root_id,
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [],
    }
    project = Project(
        project_id=uuid.uuid4(),
        root_folder_id=root_id,
        drive=drive_wrapper,
    )
    project.initialize()
    drive_wrapper.files.files["sheet-1"] = {
        "id": "sheet-1",
        "name": "Tasks",
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [root_id],
        "appProperties": {},
    }
    rows = table or [
        ["task_id", "email", "title", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
        ["t1", "a@example.com", "One", "t1", "1", "2024-01-01T00:00:00+00:00"],
    ]
    raw = MagicMock()
    raw.files.return_value = drive_wrapper.files
    drive = DriveBackend(
        MemoryDriveService(),
        project_id=str(project.project_id),
    )
    sheets = SheetsBackend(FakeSheetsService(rows))
    store = IndexStore(str(tmp_path / "indexes"))
    backend = RegistryBackend(
        project=project,
        drive=drive,
        sheets=sheets,
        scan_limit=100,
        index_store=store,
    )
    backend.bind_model(
        IndexedTask,
        RegistryEntry(
            model_name=IndexedTask.gdm_model_name(),
            resource_id="sheet-1",
            kind=ResourceKind.SPREADSHEET,
            extra={"worksheet": "Sheet1"},
        ),
    )
    return backend, project


def test_registry_index_capabilities_and_rebuild(tmp_path) -> None:
    backend, _project = _registry_with_indexes(tmp_path)
    caps = backend.capabilities()
    assert caps.supports_sidecar_index is True
    assert caps.supports_metadata_pushdown is False
    count = backend.rebuild_index(IndexedTask, "email")
    assert count == 1
    counts = backend.rebuild_model_indexes(IndexedTask)
    assert counts["email"] == 1


def test_registry_rebuild_without_store_raises() -> None:
    drive_wrapper = FakeDrive()
    project = Project(
        project_id=uuid.uuid4(),
        root_folder_id="root",
        drive=drive_wrapper,
    )
    backend = RegistryBackend(
        project=project,
        drive=MagicMock(),
        sheets=MagicMock(),
    )
    with pytest.raises(GDMError, match="Index store"):
        backend.rebuild_index(IndexedTask, "email")


def test_registry_read_by_record_key_sheet(tmp_path) -> None:
    backend, _project = _registry_with_indexes(tmp_path)
    stored = backend.read_by_record_key(IndexedTask, "t1")
    assert stored.values["email"] == "a@example.com"


def test_registry_compute_index_fingerprints_sheet(tmp_path) -> None:
    backend, _project = _registry_with_indexes(tmp_path)
    combined, parts = backend.compute_index_fingerprints(IndexedTask)
    assert len(combined) == 64
    assert parts is None


def test_registry_configure_index_store(tmp_path) -> None:
    backend, _project = _registry_with_indexes(tmp_path)
    new_store = IndexStore(str(tmp_path / "other-indexes"))
    backend.configure_index_store(new_store, stale_policy=StalePolicy.ERROR)
    assert backend.index_runtime is not None
    assert backend.index_runtime.stale_policy is StalePolicy.ERROR


def test_client_configure_and_rebuild_indexes(tmp_path) -> None:
    backend, project = _registry_with_indexes(tmp_path)
    client = GDMClient(
        project=project,
        drive=backend.drive,
        sheets=backend.sheets,
        registry_backend=backend,
        drive_service=MagicMock(),
    )
    store = client.configure_indexes(str(tmp_path / "client-indexes"))
    assert store is not None
    count = client.rebuild_index(IndexedTask, "email")
    assert count >= 0
    counts = client.rebuild_model_indexes(IndexedTask)
    assert "email" in counts


def test_client_session_stale_policy_override(tmp_path) -> None:
    backend, project = _registry_with_indexes(tmp_path)
    client = GDMClient(
        project=project,
        drive=backend.drive,
        sheets=backend.sheets,
        registry_backend=backend,
        drive_service=MagicMock(),
    )
    session = client.session(stale_policy=StalePolicy.REBUILD)
    assert session._index_runtime is not None
    assert session._index_runtime.stale_policy is StalePolicy.REBUILD


def test_registry_exec_uses_index_lookup(tmp_path) -> None:
    backend, _project = _registry_with_indexes(tmp_path)
    backend.rebuild_index(IndexedTask, "email")
    with Session(backend) as session:
        rows = session.exec(
            select(IndexedTask).where(IndexedTask.email == "a@example.com")
        ).all()
    assert len(rows) == 1
    assert rows[0].id == "t1"


def test_extract_index_lookup_or_falls_back() -> None:
    lookup, remaining = extract_index_lookup(
        IndexedTask,
        (IndexedTask.email == "x") | (IndexedTask.title == "y"),
    )
    assert lookup is None
    assert remaining is not None


def test_decode_index_key_variants() -> None:
    assert decode_index_key("s:hello") == "hello"
    assert decode_index_key("i:42") == 42
    assert decode_index_key("f:3.5") == 3.5
    assert decode_index_key("b:true") is True
    assert decode_index_key("n:null") is None


def test_registry_sheet_fingerprint_scan_limit(tmp_path) -> None:
    rows = [
        ["task_id", "email", "title", "_gdm_id", "_gdm_version", "_gdm_updated_at"],
    ]
    for index in range(3):
        rows.append([f"t{index}", f"{index}@x.com", "x", f"t{index}", "1", "2024"])
    backend, _project = _registry_with_indexes(tmp_path, table=rows)
    backend.scan_limit = 1
    with pytest.raises(ScanLimitExceeded):
        backend.compute_index_fingerprints(IndexedTask)


class _LegacyBackend:
    """Minimal backend without ``read_by_record_key`` for session fallback tests."""

    scan_call_count = 0

    def __init__(self, tmp_path) -> None:
        from gdm.backends.memory import MemoryBackend
        from gdm.indexes import IndexRuntime, IndexStore

        self._inner = MemoryBackend(
            index_runtime=IndexRuntime(store=IndexStore(str(tmp_path / "legacy")))
        )
        self._inner.seed_record(
            IndexedTask,
            {"task_id": "t1", "email": "legacy@x.com", "title": "L"},
            record_key="t1",
        )
        self.index_runtime = self._inner.index_runtime
        self.scan_limit = 1000

    def scan(self, model, *, limit=None):
        self.scan_call_count += 1
        return self._inner.scan(model, limit=limit)

    def read(self, model, locator):
        return self._inner.read(model, locator)

    def compute_index_fingerprints(self, model):
        return self._inner.compute_index_fingerprints(model)


def test_session_index_read_fallback_without_read_by_key(tmp_path) -> None:
    backend = _LegacyBackend(tmp_path)
    backend.index_runtime.rebuild_field_index(IndexedTask, "email", backend)
    with Session(backend) as session:
        backend.scan_call_count = 0
        rows = session.exec(
            select(IndexedTask).where(IndexedTask.email == "legacy@x.com")
        ).all()
    assert len(rows) == 1
    assert backend.scan_call_count == 0


def test_validate_unique_uses_scan_when_index_missing() -> None:
    from gdm.backends.memory import MemoryBackend
    from gdm.indexes import validate_unique_constraints
    from gdm.model import GDMModel

    class UniqueEmail(GDMModel, storage="sheet", source="u", mapping="partial"):
        id: str = Field(primary_key=True, source="row_id")
        email: str = Field(unique=True)

    backend = MemoryBackend()
    backend.seed_record(
        UniqueEmail,
        {"row_id": "1", "email": "dup@x.com"},
        record_key="1",
    )
    with pytest.raises(GdmIndexError, match="Unique constraint"):
        validate_unique_constraints(
            model=UniqueEmail,
            pending=[("2", None, {"id": "2", "email": "dup@x.com"})],
            backend=backend,
        )


def test_compute_live_fingerprint_generic_backend() -> None:
    from gdm.indexes import compute_live_fingerprint_parts

    class GenericBackend:
        scan_limit = 100

        def scan(self, model, *, limit=None):
            return []

    combined, parts = compute_live_fingerprint_parts(GenericBackend(), IndexedTask)
    assert len(combined) == 64
    assert parts is None


def test_decode_index_key_unknown_tag() -> None:
    assert decode_index_key("x:payload") == "payload"


def test_extract_index_lookup_rejects_two_indexed_equalities() -> None:
    class MultiIndex(GDMModel, storage="sheet", source="m", mapping="partial"):
        id: str = Field(primary_key=True, source="row_id")
        email: str = Field(index=True)
        code: str = Field(index=True)

    lookup, remaining = extract_index_lookup(
        MultiIndex,
        (MultiIndex.email == "a") & (MultiIndex.code == "b"),
    )
    assert lookup is None
    assert remaining is not None
