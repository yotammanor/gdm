"""Branch coverage for sidecar indexes."""

from __future__ import annotations

import pytest

from gdm.indexes import (
    GdmIndexError,
    IndexFingerprints,
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
)


def test_index_add_idempotent_and_unique() -> None:
    index = SidecarIndex("M", "f", False, "fp")
    index.add("x", "r1")
    index.add("x", "r1")
    assert index.lookup("x") == ("r1",)
    unique = SidecarIndex("M", "f", True, "fp")
    unique.add("x", "r1")
    with pytest.raises(GdmIndexError):
        unique.add("x", "r2")


def test_index_remove_missing_key() -> None:
    index = SidecarIndex("M", "f", False, "fp")
    index.remove("missing", "r1")
    index.add("x", "r1")
    index.remove("x", "missing")
    assert index.lookup("x") == ("r1",)


def test_replace_record() -> None:
    index = SidecarIndex("M", "f", False, "fp")
    index.add("old", "r1")
    index.replace_record("old", "new", "r1")
    assert index.lookup("new") == ("r1",)


def test_to_dict_with_fingerprints() -> None:
    fps = IndexFingerprints(drive_fingerprint="d", metadata_fingerprint="m")
    index = SidecarIndex("M", "f", False, "fp", fingerprints=fps)
    data = index.to_dict()
    assert data["fingerprints"]["drive"] == "d"


def test_canonical_key_complex() -> None:
    assert canonical_key([1, 2]).tag == "a"
    assert canonical_key({"a": 1}).tag == "o"


def test_compute_source_fingerprint_partial() -> None:
    fp = compute_source_fingerprint(
        resource_id="r",
        drive_fingerprint="d",
        metadata_fingerprint="m",
        content_fingerprint="c",
        content_indexed=True,
    )
    assert len(fp) == 64


def test_deserialize_with_fingerprints() -> None:
    index = SidecarIndex(
        model_name="M",
        field_name="f",
        unique=False,
        source_fingerprint="fp",
        entries={},
        fingerprints=IndexFingerprints(content_indexed=True, content_fingerprint="c"),
    )
    loaded = deserialize_index(serialize_index(index))
    assert loaded.fingerprints is not None
    assert loaded.fingerprints.content_indexed is True


def test_index_store_load_missing_and_delete(tmp_path) -> None:
    store = IndexStore(str(tmp_path))
    assert store.load("M", "f") is None
    index = SidecarIndex("M", "f", False, "fp")
    store.save(index)
    assert store.load("M", "f") is not None
    assert store.delete("M", "f") is True
    assert store.delete("M", "f") is False


def test_maintainer_update_record_and_lookup_policies(tmp_path) -> None:
    store = IndexStore(str(tmp_path))
    maintainer = IndexMaintainer(store)
    index = maintainer.rebuild_from_records(
        model_name="M",
        field_name="email",
        unique=False,
        records=(("r1", {"email": "a@x.com"}),),
        source_fingerprint="old",
    )
    maintainer.update_record(
        index,
        record_id="r1",
        old_payload={"email": "a@x.com"},
        new_payload={"email": "b@x.com"},
    )
    assert maintainer.lookup_with_policy(
        "M", "email", "b@x.com", live_fingerprint="old"
    ) == ("r1",)
    assert (
        maintainer.lookup_with_policy(
            "M", "email", "b@x.com", live_fingerprint="new", policy=StalePolicy.FALLBACK
        )
        is None
    )
    with pytest.raises(StaleIndexError):
        maintainer.lookup_with_policy(
            "M", "email", "b@x.com", live_fingerprint="new", policy=StalePolicy.ERROR
        )
    rebuilt = maintainer.lookup_with_policy(
        "M",
        "email",
        "b@x.com",
        live_fingerprint="new",
        policy=StalePolicy.REBUILD,
    )
    assert rebuilt == ()


def test_maintainer_skip_missing_field(tmp_path) -> None:
    store = IndexStore(str(tmp_path))
    maintainer = IndexMaintainer(store)
    index = maintainer.rebuild_from_records(
        model_name="M",
        field_name="email",
        unique=False,
        records=(("r1", {"name": "Ann"}),),
        source_fingerprint="fp",
    )
    assert index.entries == {}


def test_merge_index_entries_conflict() -> None:
    left = SidecarIndex("M", "f", False, "fp")
    right = SidecarIndex("Other", "f", False, "fp")
    with pytest.raises(GdmIndexError):
        merge_index_entries(left, right)


def test_merge_deduplicates_record_ids() -> None:
    left = SidecarIndex("M", "f", False, "fp1", {"s:a": ["1", "2"]})
    right = SidecarIndex("M", "f", False, "fp2", {"s:a": ["2", "3"]})
    merged = merge_index_entries(left, right)
    assert merged.lookup("a") == ("1", "2", "3")


def test_verify_fingerprint_rebuild_saves(tmp_path) -> None:
    store = IndexStore(str(tmp_path))
    index = SidecarIndex("M", "f", False, "old", {"s:x": ["1"]})
    store.save(index)
    maintainer = IndexMaintainer(store)
    maintainer.lookup_with_policy(
        "M", "f", "x", live_fingerprint="new", policy=StalePolicy.REBUILD
    )
    loaded = store.load("M", "f")
    assert loaded is not None
    assert loaded.source_fingerprint == "new"


def test_collection_fingerprints_combined() -> None:
    fps = compute_collection_fingerprints(
        folder_id="f",
        metadata_table_fingerprint="meta",
        content_fingerprint="body",
        content_indexed=True,
    )
    assert fps.combined() != compute_collection_fingerprints(folder_id="f").combined()
