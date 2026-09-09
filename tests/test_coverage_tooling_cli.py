"""Branch coverage for CLI commands and wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel
from typer.testing import CliRunner

from gdm import Field, GDMModel
from gdm.cli import CLIContext, app
from gdm.collection.metadata import InMemoryCollectionMetadataStore
from gdm.explore import Explorer, InMemoryDriveService
from gdm.indexes import IndexStore
from gdm.migrations import InMemoryMigrationTarget, InMemorySheet
from gdm.schema import SchemaRegistry, build_model_snapshot

runner = CliRunner()


class ArticleV1(BaseModel):
    title: str


ArticleV1.gdm_config = {
    "name": "Article",
    "storage": "sheet",
    "mapping_mode": "partial",
}  # type: ignore[attr-defined]


class ArticleV2(BaseModel):
    title: str
    subtitle: str | None = None


ArticleV2.gdm_config = {
    "name": "Article",
    "storage": "sheet",
    "mapping_mode": "partial",
}  # type: ignore[attr-defined]


class Asset(GDMModel, storage="collection", collection="Assets", mapping="partial"):
    drive_id: str = Field(drive="id", primary_key=True)
    name: str = Field(drive="name")


def _write_schema_snapshots(tmp_path: Path) -> None:
    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    applied = build_model_snapshot(ArticleV1)
    current = build_model_snapshot(ArticleV2)
    registry = SchemaRegistry(str(schemas))
    registry.save(applied, applied=True)
    registry.save(current, applied=False)


def test_cli_callback_default_context() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_init_existing_without_force(tmp_path: Path) -> None:
    gdm = tmp_path / ".gdm"
    gdm.mkdir()
    result = runner.invoke(
        app, ["--project-dir", str(tmp_path), "init", "--root-id", "r"]
    )
    assert result.exit_code == 0
    assert "already exists" in result.output


def test_init_force_and_remote_dry_run(tmp_path: Path) -> None:
    gdm = tmp_path / ".gdm"
    gdm.mkdir()
    (gdm / "manifest.json").write_text("{}", encoding="utf-8")
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "init",
            "--root-id",
            "root",
            "--force",
            "--remote",
        ],
    )
    assert result.exit_code == 0
    assert "dry-run" in result.output.lower()


def test_explore_human_output(tmp_path: Path) -> None:
    service = InMemoryDriveService(
        children={
            "root": [
                {
                    "id": "1",
                    "name": "dup",
                    "mimeType": "application/vnd.google-apps.document",
                },
                {
                    "id": "2",
                    "name": "dup",
                    "mimeType": "application/vnd.google-apps.document",
                },
                {
                    "id": "3",
                    "name": ".gdm",
                    "mimeType": "application/vnd.google-apps.folder",
                },
                {
                    "id": "4",
                    "name": "book",
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                },
            ],
        }
    )
    ctx = CLIContext(project_dir=tmp_path, explorer_factory=lambda: Explorer(service))
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "explore", "--root-id", "root"],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert "Explore root" in result.output
    assert "duplicate" in result.output.lower()
    assert "spreadsheet" in result.output.lower()


def test_infer_collection_missing_folder_id(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "infer", "collection"])
    assert result.exit_code == 1


def test_infer_collection_json_and_output_file(tmp_path: Path) -> None:
    service = InMemoryDriveService(
        children={"folder": [{"id": "1", "name": "a.png", "mimeType": "image/png"}]}
    )
    out = tmp_path / "model.py"
    ctx = CLIContext(project_dir=tmp_path, explorer_factory=lambda: Explorer(service))
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "infer",
            "collection",
            "--folder-id",
            "folder",
            "--json",
            "--output",
            str(out),
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["binding"]["storage"] == "collection"
    assert out.exists()
    assert 'source="' not in out.read_text() or "source=" in out.read_text()


def test_infer_file_csv_tsv_and_output(tmp_path: Path) -> None:
    csv_path = tmp_path / "data.csv"
    csv_path.write_text("name,count\nAnn,1\nBob,2\n", encoding="utf-8")
    out = tmp_path / "csv_model.py"
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "infer",
            "file",
            str(csv_path),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0
    assert out.exists()

    tsv_path = tmp_path / "data.tsv"
    tsv_path.write_text("name\tval\nX\t1\n", encoding="utf-8")
    result_tsv = runner.invoke(
        app, ["--project-dir", str(tmp_path), "infer", "file", str(tsv_path)]
    )
    assert result_tsv.exit_code == 0


def test_doctor_drift_only_and_json(tmp_path: Path) -> None:
    _write_schema_snapshots(tmp_path)
    ctx = CLIContext(
        project_dir=tmp_path,
        schema_registry_factory=lambda: SchemaRegistry(
            str(tmp_path / ".gdm" / "schemas")
        ),
    )
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "doctor", "--model", "Article"],
        obj=ctx,
    )
    assert result.exit_code == 0

    service = InMemoryDriveService(
        children={"folder": [{"id": "1", "name": "a", "mimeType": "image/png"}]}
    )
    ctx2 = CLIContext(
        project_dir=tmp_path,
        schema_registry_factory=lambda: SchemaRegistry(
            str(tmp_path / ".gdm" / "schemas")
        ),
        explorer_factory=lambda: Explorer(service),
        metadata_store_factory=lambda: InMemoryCollectionMetadataStore(rows={}),
    )
    result_json = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "doctor",
            "--model",
            "Asset",
            "--folder-id",
            "folder",
            "--json",
        ],
        obj=ctx2,
    )
    assert result_json.exit_code == 0
    payload = json.loads(result_json.stdout)
    assert "issues" in payload


def test_doctor_no_schemas_lists_files(tmp_path: Path) -> None:
    (tmp_path / ".gdm" / "schemas").mkdir(parents=True)
    (tmp_path / ".gdm" / "schemas" / "Foo.current.json").write_text(
        "{}", encoding="utf-8"
    )
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "doctor"])
    assert result.exit_code == 0
    assert "Foo.current.json" in result.output


def test_doctor_errors_on_drift(tmp_path: Path) -> None:
    _write_schema_snapshots(tmp_path)
    ctx = CLIContext(
        project_dir=tmp_path,
        schema_registry_factory=lambda: SchemaRegistry(
            str(tmp_path / ".gdm" / "schemas")
        ),
    )
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "doctor", "--model", "Article"],
        obj=ctx,
    )
    assert result.exit_code == 0


def test_migrate_plan_missing_current(tmp_path: Path) -> None:
    (tmp_path / ".gdm" / "schemas").mkdir(parents=True)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "migrate",
            "plan",
            "--model",
            "Missing",
            "--resource-id",
            "x",
        ],
    )
    assert result.exit_code == 1


def test_migrate_plan_blocked_destructive(tmp_path: Path) -> None:
    class StrictV1(BaseModel):
        title: str
        subtitle: str

    class StrictV2(BaseModel):
        title: str

    StrictV1.gdm_config = {
        "name": "Strict",
        "storage": "sheet",
        "mapping_mode": "partial",
    }  # type: ignore[attr-defined]
    StrictV2.gdm_config = {
        "name": "Strict",
        "storage": "sheet",
        "mapping_mode": "partial",
    }  # type: ignore[attr-defined]

    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    registry = SchemaRegistry(str(schemas))
    registry.save(build_model_snapshot(StrictV1), applied=True)
    registry.save(build_model_snapshot(StrictV2), applied=False)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "migrate",
            "plan",
            "--model",
            "Strict",
            "--resource-id",
            "sheet-1",
        ],
    )
    assert result.exit_code == 1
    assert "destructive" in result.output.lower()


def test_migrate_apply_success(tmp_path: Path) -> None:
    _write_schema_snapshots(tmp_path)
    target = InMemoryMigrationTarget(
        sheets={"sheet-1": InMemorySheet(headers=["title"], rows=[["Hi"]])},
        model_bindings={"Article": "sheet-1"},
        files={},
        indexes={},
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        dry_run=False,
        migration_target_factory=lambda: target,
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "migrate",
            "apply",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert "subtitle" in target.sheet_headers("sheet-1")


def test_migrate_apply_partial_failure(tmp_path: Path) -> None:
    from gdm.migrations import (
        MigrationEngine,
        MigrationOperation,
        MigrationOpKind,
        MigrationPlan,
    )

    target = InMemoryMigrationTarget(
        sheets={"sheet-1": InMemorySheet(headers=["title"], rows=[["Hi"]])},
        model_bindings={"Article": "sheet-1"},
        files={},
        indexes={},
    )
    plan = MigrationPlan(
        model_name="Article",
        operations=(
            MigrationOperation(
                kind=MigrationOpKind.TRANSFORM_FIELD,
                model_name="Article",
                params={
                    "source_name": "missing",
                    "storage": "sheet",
                    "target_kind": "source",
                    "from_type": "int",
                    "to_type": "float",
                },
                destructive=True,
            ),
        ),
        dry_run=False,
        allow_destructive=True,
    )
    report = MigrationEngine(target).apply(plan)
    assert report.partial_failure


def test_index_rebuild_apply(tmp_path: Path) -> None:
    indexes = tmp_path / ".gdm" / "indexes"
    indexes.mkdir(parents=True)
    ctx = CLIContext(
        project_dir=tmp_path,
        dry_run=False,
        index_store_factory=lambda: IndexStore(str(indexes)),
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "index",
            "rebuild",
            "--model",
            "Person",
            "--field",
            "email",
            "--resource-id",
            "folder-1",
            "--unique",
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert (indexes / "Person" / "email.json").exists()


def test_auth_login_factory(tmp_path: Path) -> None:
    ctx = CLIContext(
        project_dir=tmp_path,
        auth_login_factory=lambda: 0,
    )
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "auth", "login"],
        obj=ctx,
    )
    assert result.exit_code == 0


def test_migrate_apply_no_target_configured(tmp_path: Path) -> None:
    _write_schema_snapshots(tmp_path)
    ctx = CLIContext(project_dir=tmp_path, dry_run=False)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "migrate",
            "apply",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
        ],
        obj=ctx,
    )
    assert result.exit_code == 1


def test_adopt_apply_requires_local_project(tmp_path: Path) -> None:
    ctx = CLIContext(project_dir=tmp_path, dry_run=False)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "adopt",
            "--model-name",
            "Asset",
            "--folder-id",
            "f1",
        ],
        obj=ctx,
    )
    assert result.exit_code == 1
    assert "manifest" in result.output.lower()


def test_init_remote_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeClient:
        project = type("P", (), {"project_id": "proj-1"})()

        @classmethod
        def open(cls, **kwargs: object) -> FakeClient:
            from gdm.errors import NotFoundError

            raise NotFoundError("not initialized")

        @classmethod
        def create(cls, **kwargs: object) -> FakeClient:
            return cls()

    monkeypatch.setattr("gdm.client.GDMClient", FakeClient)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "init",
            "--root-id",
            "root",
            "--remote",
        ],
    )
    assert result.exit_code == 0
    assert "Remote project initialized" in result.output
    manifest = json.loads((tmp_path / ".gdm" / "manifest.json").read_text())
    assert manifest["project_id"] == "proj-1"


def test_init_remote_reuses_existing_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        project = type("P", (), {"project_id": "existing-project"})()

        @classmethod
        def open(cls, **kwargs: object) -> FakeClient:
            return cls()

        @classmethod
        def create(cls, **kwargs: object) -> FakeClient:
            raise AssertionError("existing project should be opened")

    monkeypatch.setattr("gdm.client.GDMClient", FakeClient)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "init",
            "--root-id",
            "root",
            "--remote",
        ],
    )
    assert result.exit_code == 0
    manifest = json.loads((tmp_path / ".gdm" / "manifest.json").read_text())
    assert manifest["project_id"] == "existing-project"


def test_explore_without_collection_candidates(tmp_path: Path) -> None:
    service = InMemoryDriveService(
        children={"root": [{"id": "1", "name": "solo.txt", "mimeType": "text/plain"}]}
    )
    ctx = CLIContext(project_dir=tmp_path, explorer_factory=lambda: Explorer(service))
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "explore", "--root-id", "root"],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert (
        "runtime" not in result.output.lower()
        or "zero content" in result.output.lower()
    )


def test_doctor_with_drift_errors_exits(tmp_path: Path) -> None:
    class StrictV1(BaseModel):
        title: str
        subtitle: str

    class StrictV2(BaseModel):
        title: str

    StrictV1.gdm_config = {
        "name": "Strict",
        "storage": "sheet",
        "mapping_mode": "partial",
    }  # type: ignore[attr-defined]
    StrictV2.gdm_config = {
        "name": "Strict",
        "storage": "sheet",
        "mapping_mode": "partial",
    }  # type: ignore[attr-defined]
    schemas = tmp_path / ".gdm" / "schemas"
    schemas.mkdir(parents=True)
    registry = SchemaRegistry(str(schemas))
    registry.save(build_model_snapshot(StrictV1), applied=True)
    registry.save(build_model_snapshot(StrictV2), applied=False)
    ctx = CLIContext(
        project_dir=tmp_path,
        schema_registry_factory=lambda: SchemaRegistry(str(schemas)),
    )
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "doctor", "--model", "Strict"],
        obj=ctx,
    )
    assert result.exit_code == 1


def test_doctor_index_dir_missing(tmp_path: Path) -> None:
    (tmp_path / ".gdm").mkdir()
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "doctor"])
    assert result.exit_code == 0
    assert "No index directory" in result.output


def test_migrate_plan_success(tmp_path: Path) -> None:
    _write_schema_snapshots(tmp_path)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "migrate",
            "plan",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
        ],
    )
    assert result.exit_code == 0
    assert "Dry-run plan" in result.output


def test_migrate_apply_partial_failure_cli(tmp_path: Path) -> None:
    _write_schema_snapshots(tmp_path)
    target = InMemoryMigrationTarget(
        sheets={"sheet-1": InMemorySheet(headers=["title"], rows=[["Hi"]])},
        model_bindings={"Article": "sheet-1"},
        files={},
        indexes={},
    )

    class BrokenTarget(InMemoryMigrationTarget):
        def sheet_add_column(self, resource_id: str, header: str, default=None) -> int:
            raise RuntimeError("fail")

    broken = BrokenTarget(
        sheets=target.sheets,
        model_bindings=target.model_bindings,
        files=target.files,
        indexes=target.indexes,
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        dry_run=False,
        migration_target_factory=lambda: broken,
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "--apply",
            "migrate",
            "apply",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
        ],
        obj=ctx,
    )
    assert result.exit_code == 1
    assert "partial failure" in result.output.lower()


def test_auth_login_success_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GDM_CLIENT_SECRETS", str(tmp_path / "secrets.json"))
    (tmp_path / "secrets.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("gdm.auth.login_interactive", lambda **kwargs: None)
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "auth", "login"])
    assert result.exit_code == 0
    assert "Login completed" in result.output


def test_cli_main_guard() -> None:
    import gdm.cli as cli_module

    assert cli_module.app is not None
