"""Tests for sidecar indexes."""

from __future__ import annotations

import pytest

from gdm.indexes import (
    GdmIndexError,
    IndexMaintainer,
    IndexStore,
    SidecarIndex,
    StaleIndexError,
    StalePolicy,
    canonical_key,
    compute_collection_fingerprints,
    compute_source_fingerprint,
    deserialize_index,
    merge_index_entries,
    serialize_index,
    verify_fingerprint,
)


def test_canonical_key_types() -> None:
    assert canonical_key("hello").key() == "s:hello"
    assert canonical_key(42).key() == "i:42"
    assert canonical_key(3.5).key() == "f:3.5"
    assert canonical_key(True).key() == "b:true"
    assert canonical_key(None).key() == "n:null"


def test_collection_fingerprints_and_content_flag() -> None:
    fps = compute_collection_fingerprints(
        folder_id="folder-1",
        drive_version="3",
        metadata_table_fingerprint="meta",
        content_indexed=False,
    )
    assert fps.content_indexed is False
    combined = fps.combined()
    fps_with_content = compute_collection_fingerprints(
        folder_id="folder-1",
        content_fingerprint="body",
        content_indexed=True,
    )
    assert fps_with_content.combined() != combined


def test_index_serialization_includes_locator_kind() -> None:
    index = SidecarIndex(
        model_name="Person",
        field_name="email",
        unique=False,
        source_fingerprint="abc",
        entries={"s:a@x.com": ["drive-id-2", "drive-id-1"]},
        locator_kind="drive_file_id",
    )
    first = serialize_index(index)
    loaded = deserialize_index(first)
    assert loaded.locator_kind == "drive_file_id"
    second = serialize_index(loaded)
    assert first == second


def test_unique_index_violation() -> None:
    index = SidecarIndex(
        model_name="Person",
        field_name="email",
        unique=True,
        source_fingerprint="fp",
    )
    index.add("a@example.com", "one")
    with pytest.raises(GdmIndexError):
        index.add("a@example.com", "two")


def test_lookup_and_remove() -> None:
    index = SidecarIndex(
        model_name="Person",
        field_name="name",
        unique=False,
        source_fingerprint="fp",
    )
    index.add("Ann", "r1")
    assert index.lookup("Ann") == ("r1",)
    index.remove("Ann", "r1")
    assert index.lookup("Ann") == ()


def test_stale_policies() -> None:
    index = SidecarIndex(
        model_name="M",
        field_name="f",
        unique=False,
        source_fingerprint="old",
        entries={"s:x": ["1"]},
    )
    assert verify_fingerprint(index, "old") is index
    assert verify_fingerprint(index, "new", policy=StalePolicy.FALLBACK) is None
    with pytest.raises(StaleIndexError):
        verify_fingerprint(index, "new", policy=StalePolicy.ERROR)
    rebuilt = verify_fingerprint(index, "new", policy=StalePolicy.REBUILD)
    assert rebuilt is not None
    assert rebuilt.entries == {}


def test_source_fingerprint_stable() -> None:
    fp1 = compute_source_fingerprint(resource_id="abc", version=3, row_count=10)
    fp2 = compute_source_fingerprint(resource_id="abc", version=3, row_count=10)
    assert fp1 == fp2


def test_index_store_roundtrip(tmp_path) -> None:
    store = IndexStore(str(tmp_path / "indexes"))
    index = SidecarIndex(
        model_name="Person",
        field_name="email",
        unique=True,
        source_fingerprint="fp",
        entries={"s:a@example.com": ["id1"]},
    )
    store.save(index)
    loaded = store.load("Person", "email")
    assert loaded is not None
    assert loaded.entries["s:a@example.com"] == ["id1"]


def test_maintainer_rebuild(tmp_path) -> None:
    store = IndexStore(str(tmp_path / "indexes"))
    maintainer = IndexMaintainer(store)
    index = maintainer.rebuild_from_records(
        model_name="Person",
        field_name="email",
        unique=False,
        records=(
            ("id1", {"email": "a@example.com"}),
            ("id2", {"email": "b@example.com"}),
        ),
        source_fingerprint="fp",
    )
    assert index.lookup("a@example.com") == ("id1",)


def test_merge_index_entries() -> None:
    left = SidecarIndex("M", "f", False, "fp", {"s:a": ["1"]})
    right = SidecarIndex("M", "f", False, "fp", {"s:b": ["2"]})
    merged = merge_index_entries(left, right)
    assert merged.lookup("a") == ("1",)
    assert merged.lookup("b") == ("2",)
