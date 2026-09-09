"""Runtime sidecar index integration for query and commit paths."""

from __future__ import annotations

import pytest

from gdm.backends.memory import MemoryBackend
from gdm.fields import Field
from gdm.indexes import (
    GdmIndexError,
    IndexMaintainer,
    IndexRuntime,
    IndexStore,
    StaleIndexError,
    StalePolicy,
)
from gdm.model import GDMModel
from gdm.query import extract_index_lookup, select
from gdm.session import Session


class SheetPerson(GDMModel, storage="sheet", source="people", mapping="partial"):
    id: str = Field(primary_key=True, source="person_id")
    email: str = Field(index=True)
    name: str


class UniqueTag(GDMModel, storage="sheet", source="tags", mapping="partial"):
    id: str = Field(primary_key=True, source="tag_id")
    slug: str = Field(unique=True)
    label: str


class CollectionAsset(GDMModel, storage="collection", mapping="partial"):
    id: str = Field(primary_key=True, drive="id")
    campaign: str = Field(metadata="campaign", index=True)
    title: str = Field(metadata="title")


def make_indexed_backend(tmp_path) -> tuple[MemoryBackend, IndexRuntime]:
    store = IndexStore(str(tmp_path / "indexes"))
    runtime = IndexRuntime(store=store)
    backend = MemoryBackend(index_runtime=runtime)
    return backend, runtime


def _seed_sheet_person(backend: MemoryBackend, **values: object) -> None:
    payload = {
        "person_id": values.get("id", "p1"),
        "email": values.get("email", "a@example.com"),
        "name": values.get("name", "Ann"),
    }
    backend.seed_record(SheetPerson, payload, record_key=str(payload["person_id"]))


def test_extract_index_lookup_composes_with_local_filter() -> None:
    lookup, remaining = extract_index_lookup(
        SheetPerson,
        (SheetPerson.email == "a@example.com") & (SheetPerson.name.startswith("A")),
    )
    assert lookup is not None
    assert lookup.field_name == "email"
    assert lookup.value == "a@example.com"
    assert remaining is not None


def test_exec_uses_index_and_skips_scan(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    _seed_sheet_person(backend, id="p1", email="find-me@x.com", name="One")
    _seed_sheet_person(backend, id="p2", email="other@x.com", name="Two")
    runtime.rebuild_field_index(SheetPerson, "email", backend)

    with Session(backend) as session:
        backend.scan_call_count = 0
        rows = session.exec(
            select(SheetPerson).where(SheetPerson.email == "find-me@x.com")
        ).all()
        assert backend.scan_call_count == 0
        assert len(rows) == 1
        assert rows[0].id == "p1"


def test_exec_falls_back_to_scan_when_index_missing(tmp_path) -> None:
    backend, _ = make_indexed_backend(tmp_path)
    _seed_sheet_person(backend, id="p1", email="find-me@x.com")

    with Session(backend) as session:
        backend.scan_call_count = 0
        rows = session.exec(
            select(SheetPerson).where(SheetPerson.email == "find-me@x.com")
        ).all()
        assert backend.scan_call_count == 1
        assert len(rows) == 1


def test_exec_falls_back_when_index_stale_under_fallback(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    _seed_sheet_person(backend, id="p1", email="stale@x.com")
    runtime.rebuild_field_index(SheetPerson, "email", backend)
    backend.mutate_external(
        SheetPerson,
        "p1",
        values={"email": "changed@x.com"},
        bump_version=True,
    )

    with Session(backend) as session:
        backend.scan_call_count = 0
        rows = session.exec(
            select(SheetPerson).where(SheetPerson.email == "changed@x.com")
        ).all()
        assert backend.scan_call_count == 1
        assert rows[0].email == "changed@x.com"


def test_exec_stale_error_policy_raises(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    runtime.stale_policy = StalePolicy.ERROR
    _seed_sheet_person(backend, id="p1", email="stale@x.com")
    runtime.rebuild_field_index(SheetPerson, "email", backend)
    backend.mutate_external(
        SheetPerson,
        "p1",
        values={"email": "changed@x.com"},
        bump_version=True,
    )

    with Session(backend) as session, pytest.raises(StaleIndexError):
        session.exec(select(SheetPerson).where(SheetPerson.email == "changed@x.com"))


def test_exec_rebuild_policy_rebuilds_then_serves(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    runtime.stale_policy = StalePolicy.REBUILD
    _seed_sheet_person(backend, id="p1", email="fresh@x.com")
    store = runtime.store
    maintainer = IndexMaintainer(store)
    maintainer.rebuild_from_records(
        model_name=SheetPerson.gdm_model_name(),
        field_name="email",
        unique=False,
        records=(("p1", {"email": "old@x.com"}),),
        source_fingerprint="stale-fingerprint",
        locator_kind="sheet_row_pk",
    )

    with Session(backend) as session:
        backend.scan_call_count = 0
        rows = session.exec(
            select(SheetPerson).where(SheetPerson.email == "fresh@x.com")
        ).all()
        assert len(rows) == 1
        assert backend.scan_call_count == 1
        backend.scan_call_count = 0
        rows_again = session.exec(
            select(SheetPerson).where(SheetPerson.email == "fresh@x.com")
        ).all()
        assert backend.scan_call_count == 0
        assert len(rows_again) == 1


def test_rebuild_rejects_source_change_during_scan(tmp_path, monkeypatch) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    _seed_sheet_person(backend, id="p1", email="before@x.com")
    original_scan = backend.scan
    changed = False

    def changing_scan(*args, **kwargs):
        nonlocal changed
        records = original_scan(*args, **kwargs)
        if not changed:
            changed = True
            backend.mutate_external(
                SheetPerson,
                "p1",
                values={"email": "after@x.com"},
                bump_version=True,
            )
        return records

    monkeypatch.setattr(backend, "scan", changing_scan)
    with pytest.raises(StaleIndexError, match="changed while rebuilding"):
        runtime.rebuild_field_index(SheetPerson, "email", backend)


def test_exec_unindexed_predicate_scans(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    _seed_sheet_person(backend, id="p1", name="Alpha")
    runtime.rebuild_field_index(SheetPerson, "email", backend)

    with Session(backend) as session:
        backend.scan_call_count = 0
        rows = session.exec(
            select(SheetPerson).where(SheetPerson.name == "Alpha")
        ).all()
        assert backend.scan_call_count == 1
        assert len(rows) == 1


def test_exec_preserves_order_offset_limit(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    for index in range(4):
        _seed_sheet_person(
            backend,
            id=f"p{index}",
            email="batch@x.com",
            name=f"Name-{index}",
        )
    runtime.rebuild_field_index(SheetPerson, "email", backend)

    with Session(backend) as session:
        rows = session.exec(
            select(SheetPerson)
            .where(SheetPerson.email == "batch@x.com")
            .order_by(SheetPerson.name)
            .offset(1)
            .limit(2)
        ).all()
    assert [row.name for row in rows] == ["Name-1", "Name-2"]


def test_commit_unique_violation_before_write(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    backend.seed_record(
        UniqueTag,
        {"tag_id": "1", "slug": "taken", "label": "One"},
    )
    runtime.rebuild_field_index(UniqueTag, "slug", backend)

    with Session(backend) as session:
        tag = UniqueTag(id="2", slug="taken", label="Two")
        session.add(tag)
        with pytest.raises(GdmIndexError, match="Unique constraint"):
            session.commit()

    assert len(backend.scan(UniqueTag)) == 1


def test_commit_unique_violation_after_external_edit(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    backend.seed_record(
        UniqueTag,
        {"tag_id": "1", "slug": "old", "label": "One"},
    )
    runtime.rebuild_field_index(UniqueTag, "slug", backend)
    backend.mutate_external(
        UniqueTag,
        "1",
        values={"slug": "taken"},
        bump_version=True,
    )

    with Session(backend) as session:
        session.add(UniqueTag(id="2", slug="taken", label="Two"))
        with pytest.raises(GdmIndexError, match="Unique constraint"):
            session.commit()

    assert len(backend.scan(UniqueTag)) == 1


def test_commit_unique_pending_collision(tmp_path) -> None:
    backend, _ = make_indexed_backend(tmp_path)

    with Session(backend) as session:
        session.add(UniqueTag(id="1", slug="dup", label="A"))
        session.add(UniqueTag(id="2", slug="dup", label="B"))
        with pytest.raises(GdmIndexError, match="Unique constraint"):
            session.commit()


def test_commit_maintains_index_incrementally(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    _seed_sheet_person(backend, id="p1", email="old@x.com")
    runtime.rebuild_field_index(SheetPerson, "email", backend)

    with Session(backend) as session:
        person = session.exec(
            select(SheetPerson).where(SheetPerson.email == "old@x.com")
        ).one()
        person.email = "new@x.com"
        session.add(person)
        session.commit()

    loaded = runtime.store.load(SheetPerson.gdm_model_name(), "email")
    assert loaded is not None
    assert loaded.lookup("new@x.com") == ("p1",)
    assert loaded.lookup("old@x.com") == ()
    assert loaded.source_fingerprint == compute_live_fingerprint(backend, SheetPerson)


def test_delete_updates_index(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    _seed_sheet_person(backend, id="p1", email="gone@x.com")
    runtime.rebuild_field_index(SheetPerson, "email", backend)

    with Session(backend) as session:
        person = session.get(SheetPerson, "p1")
        assert person is not None
        session.delete(person)
        session.commit()

    loaded = runtime.store.load(SheetPerson.gdm_model_name(), "email")
    assert loaded is not None
    assert loaded.lookup("gone@x.com") == ()


def test_memory_backend_without_index_runtime_unchanged() -> None:
    backend = MemoryBackend()
    _seed_sheet_person(backend, id="p1", email="solo@x.com")

    with Session(backend) as session:
        rows = session.exec(
            select(SheetPerson).where(SheetPerson.email == "solo@x.com")
        ).all()
    assert len(rows) == 1
    assert backend.capabilities().supports_sidecar_index is False


def test_rebuild_model_indexes_api(tmp_path) -> None:
    backend, runtime = make_indexed_backend(tmp_path)
    _seed_sheet_person(backend, id="p1", email="api@x.com")
    counts = runtime.rebuild_model_indexes(SheetPerson, backend)
    assert counts["email"] == 1


def compute_live_fingerprint(backend: MemoryBackend, model: type[GDMModel]) -> str:
    fingerprint, _parts = backend.compute_index_fingerprints(model)
    return fingerprint


def test_collection_fingerprint_parts_change_on_metadata_edit(tmp_path) -> None:
    backend, _ = make_indexed_backend(tmp_path)
    stored = backend.seed_record(
        CollectionAsset,
        {"campaign": "c1", "title": "A"},
        record_key="file-1",
        version="1",
    )
    stored.metadata_fingerprint = "meta-v1"
    resource = backend.ensure_resource(CollectionAsset)
    resource.records["file-1"] = stored
    fp1, parts1 = backend.compute_index_fingerprints(CollectionAsset)
    assert parts1 is not None
    stored.metadata_fingerprint = "meta-v2"
    resource.records["file-1"] = stored
    fp2, _parts2 = backend.compute_index_fingerprints(CollectionAsset)
    assert fp1 != fp2
