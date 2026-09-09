"""Structured file codec round-trip tests via RegistryBackend."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from gdm.backends.drive import NATIVE_RICH_MIMES, DriveBackend
from gdm.backends.registry import RegistryBackend
from gdm.backends.sheets import SheetsBackend
from gdm.errors import (
    InvalidModelConfigError,
    MissingOptionalDependencyError,
    UnsupportedOperationError,
)
from gdm.fields import Field
from gdm.infer import (
    exec_generated_model,
    generate_model_code,
    infer_from_json_text,
    infer_from_text_lines,
)
from gdm.model import GDMModel
from gdm.project import Project, RegistryEntry, ResourceKind
from gdm.session import Session
from tests.fakes import FakeDrive, FakeSheetsService, MemoryDriveService


class JsonNote(GDMModel, storage="file", codec="json", mapping="permissive"):
    id: str = Field(primary_key=True)
    title: str
    body: str


class TextDoc(GDMModel, storage="file", codec="text", mapping="partial"):
    id: str = Field(primary_key=True)
    text: str


class BinaryBlob(GDMModel, storage="file", codec="bytes", mime="application/pdf"):
    id: str = Field(primary_key=True)
    data: bytes


class YamlConfig(GDMModel, storage="file", codec="yaml", mapping="permissive"):
    id: str = Field(primary_key=True)
    name: str
    enabled: bool = True


@pytest.fixture
def registry_backend() -> RegistryBackend:
    drive_files = MemoryDriveService()
    project_id = uuid.uuid4()
    drive = DriveBackend(drive_files, project_id=str(project_id))
    drive._download_bytes = (  # type: ignore[method-assign]
        lambda meta: drive_files.files.content.get(meta.file_id, b"")
    )

    drive_wrapper = FakeDrive()
    root_id = "codec-root"
    drive_wrapper.files.files[root_id] = {
        "id": root_id,
        "name": "Root",
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [],
    }
    project = Project(
        project_id=project_id,
        root_folder_id=root_id,
        drive=drive_wrapper,
    )
    project.initialize()
    folder_id = project.folder_id_for("codecs")
    for model in (JsonNote, TextDoc, BinaryBlob, YamlConfig):
        project.register_resource(
            RegistryEntry(
                model_name=model.gdm_model_name(),
                resource_id=folder_id,
                kind=ResourceKind.FOLDER,
            )
        )
    sheets = SheetsBackend(FakeSheetsService([]))
    return RegistryBackend(project=project, drive=drive, sheets=sheets, scan_limit=50)


def test_text_codec_requires_single_body_field() -> None:
    with pytest.raises(InvalidModelConfigError, match="exactly one body field"):

        class BadText(GDMModel, storage="file", codec="text"):
            id: str = Field(primary_key=True)
            a: str
            b: str


def test_bytes_codec_requires_bytes_field() -> None:
    with pytest.raises(InvalidModelConfigError, match="typed bytes"):

        class BadBytes(GDMModel, storage="file", codec="bytes"):
            id: str = Field(primary_key=True)
            data: str


def test_json_codec_session_roundtrip(registry_backend: RegistryBackend) -> None:
    with Session(registry_backend) as session:
        note = JsonNote(id="n1", title="Hello", body="World")
        session.add(note)
        session.commit()
        loaded = session.get(JsonNote, "n1")
        assert loaded is not None
        assert loaded.title == "Hello"
        assert loaded.body == "World"

    records = registry_backend.scan(JsonNote)
    assert len(records) == 1
    file_id = records[0].locator.resource_id
    meta = registry_backend.drive.get_metadata(file_id)
    assert meta.app_properties["gdm_model"] == JsonNote.gdm_model_name()
    assert meta.app_properties["gdm_id"] == "n1"


def test_json_permissive_preserves_unknown_keys(
    registry_backend: RegistryBackend,
) -> None:
    with Session(registry_backend) as session:
        note = JsonNote(id="n2", title="T", body="B")
        session.add(note)
        session.commit()

    record = registry_backend.scan(JsonNote)[0]
    file_id = record.locator.resource_id
    payload = registry_backend.drive.read_json(file_id)
    payload["extra_key"] = "kept"
    registry_backend.drive.write_json(
        parent_id=registry_backend._file_parent_id(JsonNote),
        model_name=JsonNote.gdm_model_name(),
        record_id="n2",
        payload=payload,
        file_id=file_id,
    )

    with Session(registry_backend) as session:
        loaded = session.get(JsonNote, "n2")
        assert loaded is not None
        assert loaded.gdm_extra.get("extra_key") == "kept"


def test_text_codec_session_roundtrip(registry_backend: RegistryBackend) -> None:
    body = "Complete UTF-8 document body.\nSecond line."
    with Session(registry_backend) as session:
        doc = TextDoc(id="t1", text=body)
        session.add(doc)
        session.commit()
        loaded = session.get(TextDoc, "t1")
        assert loaded is not None
        assert loaded.text == body

    file_id = registry_backend.scan(TextDoc)[0].locator.resource_id
    meta = registry_backend.drive.get_metadata(file_id)
    assert meta.app_properties["gdm_id"] == "t1"
    assert meta.mime_type == "text/plain"
    assert registry_backend.drive.read_text_body(file_id) == body


def test_bytes_codec_session_roundtrip(registry_backend: RegistryBackend) -> None:
    payload = b"\x00\x01pdf-bytes"
    with Session(registry_backend) as session:
        blob = BinaryBlob(id="b1", data=payload)
        session.add(blob)
        session.commit()
        loaded = session.get(BinaryBlob, "b1")
        assert loaded is not None
        assert loaded.data == payload

    file_id = registry_backend.scan(BinaryBlob)[0].locator.resource_id
    meta = registry_backend.drive.get_metadata(file_id)
    assert meta.mime_type == "application/pdf"
    assert registry_backend.drive.read_file_body(file_id) == payload


def test_yaml_codec_session_roundtrip(registry_backend: RegistryBackend) -> None:
    pytest.importorskip("yaml")
    with Session(registry_backend) as session:
        cfg = YamlConfig(id="y1", name="prod", enabled=False)
        session.add(cfg)
        session.commit()
        loaded = session.get(YamlConfig, "y1")
        assert loaded is not None
        assert loaded.name == "prod"
        assert loaded.enabled is False


def test_yaml_missing_extra_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    from gdm.codecs import decode_body

    real_import = builtins.__import__

    def blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "yaml":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    class NeedsYaml(GDMModel, storage="file", codec="yaml"):
        id: str = Field(primary_key=True)
        x: str

    with pytest.raises(MissingOptionalDependencyError, match="yaml"):
        decode_body(NeedsYaml, b"name: prod\n")


def test_native_workspace_rejects_codec_write(
    registry_backend: RegistryBackend,
) -> None:
    parent = registry_backend._file_parent_id(JsonNote)
    native_id = registry_backend.drive.service.files.create(
        body={
            "name": "Doc",
            "mimeType": next(iter(NATIVE_RICH_MIMES)),
            "parents": [parent],
        },
    )["id"]
    from gdm.backends.base import RecordLocator, StoredRecord

    stored = StoredRecord(
        locator=RecordLocator(storage="file", resource_id=native_id, record_key="x"),
        values={"id": "x", "title": "t", "body": "b"},
        unknown={},
        version="1",
        fingerprint=None,
    )
    with pytest.raises(UnsupportedOperationError, match="Native Workspace"):
        registry_backend.write(
            JsonNote,
            stored,
            expected_version=None,
            create=False,
        )


def test_write_preserves_unrelated_app_properties(
    registry_backend: RegistryBackend,
) -> None:
    with Session(registry_backend) as session:
        doc = TextDoc(id="t2", text="v1")
        session.add(doc)
        session.commit()

    file_id = registry_backend.scan(TextDoc)[0].locator.resource_id
    record = registry_backend.drive.service.files.records[file_id]
    record.setdefault("appProperties", {})["custom_flag"] = "yes"
    registry_backend.drive.service.files.records[file_id] = record

    with Session(registry_backend) as session:
        loaded = session.get(TextDoc, "t2")
        assert loaded is not None
        loaded.text = "v2"
        session.commit()

    updated = registry_backend.drive.get_metadata(file_id)
    assert updated.app_properties.get("custom_flag") == "yes"
    assert updated.app_properties["gdm_id"] == "t2"


def test_inferred_json_and_text_models_compile_and_bind() -> None:
    json_code = generate_model_code(
        infer_from_json_text('{"title": "Hi", "count": 1}', model_name="InferredJson")
    )
    json_model = exec_generated_model(json_code, model_name="InferredJson")
    assert json_model.gdm_storage() == "file"
    assert json_model.gdm_codec() == "json"

    text_code = generate_model_code(
        infer_from_text_lines(["full body"], model_name="InferredText")
    )
    text_model = exec_generated_model(text_code, model_name="InferredText")
    assert text_model.gdm_storage() == "file"
    assert text_model.gdm_codec() == "text"


def test_text_update_roundtrip(registry_backend: RegistryBackend) -> None:
    with Session(registry_backend) as session:
        doc = TextDoc(id="t3", text="initial")
        session.add(doc)
        session.commit()
        doc2 = session.get(TextDoc, "t3")
        assert doc2 is not None
        doc2.text = "updated"
        session.commit()
        doc3 = session.get(TextDoc, "t3")
        assert doc3 is not None
        assert doc3.text == "updated"


def test_delete_json_codec_record(registry_backend: RegistryBackend) -> None:
    with Session(registry_backend) as session:
        note = JsonNote(id="del1", title="X", body="Y")
        session.add(note)
        session.commit()
        file_id = note.gdm_locator.resource_id
        session.delete(note)
        session.commit()

    with pytest.raises(KeyError):
        registry_backend.drive.service.files.records[file_id]
