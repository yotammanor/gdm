"""Tests for Typer CLI."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from gdm.cli import CLIContext, app
from gdm.collection.metadata import InMemoryCollectionMetadataStore
from gdm.explore import Explorer, InMemoryDriveService

runner = CliRunner()


def test_help_shows_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in (
        "init",
        "explore",
        "infer",
        "doctor",
        "migrate",
        "index",
        "auth",
        "adopt",
    ):
        assert name in result.stdout


def test_init_creates_metadata(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "init", "--root-id", "root123"],
    )
    assert result.exit_code == 0
    manifest = tmp_path / ".gdm" / "manifest.json"
    assert manifest.exists()
    data = json.loads(manifest.read_text())
    assert data["root_folder_id"] == "root123"


def test_explore_with_injected_service(tmp_path: Path) -> None:
    service = InMemoryDriveService(
        children={
            "root": [
                {"id": "1", "name": "a", "mimeType": "text/plain", "size": "5"},
                {
                    "id": "2",
                    "name": "folder",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            ],
            "2": [
                {"id": "3", "name": "nested", "mimeType": "image/png"},
            ],
        }
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        explorer_factory=lambda: Explorer(service),
    )
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "explore", "--root-id", "root", "--json"],
        obj=ctx,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["total_count"] >= 2
    assert payload["zero_content_reads"] is True
    assert "collection_candidates" in payload


def test_infer_collection_with_injected_explorer(tmp_path: Path) -> None:
    service = InMemoryDriveService(
        children={
            "folder": [
                {"id": "1", "name": "a.png", "mimeType": "image/png"},
            ]
        }
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        explorer_factory=lambda: Explorer(service),
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "infer",
            "collection",
            "--folder-id",
            "folder",
            "--model-name",
            "Asset",
        ],
        obj=ctx,
    )
    assert result.exit_code == 0, result.output
    assert "storage: collection" in result.output
    assert 'storage="collection"' in result.output
    assert "gdm_config" not in result.output


def test_infer_file_from_json(tmp_path: Path) -> None:
    source = tmp_path / "doc.json"
    source.write_text(json.dumps({"title": "Hi"}), encoding="utf-8")
    result = runner.invoke(
        app,
        ["--project-dir", str(tmp_path), "infer", "file", str(source)],
    )
    assert result.exit_code == 0
    assert "InferredModel" in result.stdout


def test_adopt_dry_run_default(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "adopt",
            "--model-name",
            "Asset",
            "--folder-id",
            "folder-1",
        ],
    )
    assert result.exit_code == 0
    assert "Dry-run" in result.output
    assert "metadata_only: True" in result.output


def test_adopt_apply_with_binder(tmp_path: Path) -> None:
    ctx = CLIContext(
        project_dir=tmp_path,
        dry_run=False,
        adopt_binder_factory=lambda: lambda plan: {"folder_id": plan.folder_id},
    )
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
            "folder-1",
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert "folder-1" in result.output


def test_doctor_missing_project(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "doctor"])
    assert result.exit_code == 1


def test_doctor_collection_with_injected_deps(tmp_path: Path) -> None:
    (tmp_path / ".gdm" / "schemas").mkdir(parents=True)
    service = InMemoryDriveService(
        children={"folder": [{"id": "1", "name": "a", "mimeType": "image/png"}]}
    )
    ctx = CLIContext(
        project_dir=tmp_path,
        explorer_factory=lambda: Explorer(service),
        metadata_store_factory=lambda: InMemoryCollectionMetadataStore(rows={}),
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "doctor",
            "--model",
            "Asset",
            "--folder-id",
            "folder",
        ],
        obj=ctx,
    )
    assert result.exit_code == 0
    assert "metadata-only" in result.output.lower()


def test_migrate_apply_refuses_dry_run(tmp_path: Path) -> None:
    gdm = tmp_path / ".gdm" / "schemas"
    gdm.mkdir(parents=True)
    (gdm / "Article.current.json").write_text(
        '{"model_name":"Article","storage":"sheet","mapping_mode":"partial",'
        '"format_version":1,"pydantic_schema":{},"fields":[],"system_columns":[],'
        '"content_hash":"abc"}',
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "migrate",
            "apply",
            "--model",
            "Article",
            "--resource-id",
            "sheet-1",
        ],
    )
    assert result.exit_code == 1
    assert "dry-run" in result.output.lower()


def test_migrate_plan_labels_metadata_only(tmp_path: Path) -> None:
    gdm = tmp_path / ".gdm" / "schemas"
    gdm.mkdir(parents=True)
    snapshot = (
        '{"model_name":"Article","storage":"collection","mapping_mode":"partial",'
        '"format_version":2,"pydantic_schema":{},"fields":[],"system_columns":[],'
        '"content_hash":"abc","collection_path":"Assets","mime_patterns":[]}'
    )
    (gdm / "Article.current.json").write_text(snapshot, encoding="utf-8")
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
            "folder-1",
        ],
    )
    assert result.exit_code == 0
    assert "metadata-only" in result.output or "Dry-run" in result.output


def test_index_rebuild_dry_run(tmp_path: Path) -> None:
    (tmp_path / ".gdm" / "indexes").mkdir(parents=True)
    result = runner.invoke(
        app,
        [
            "--project-dir",
            str(tmp_path),
            "index",
            "rebuild",
            "--model",
            "Person",
            "--field",
            "email",
            "--resource-id",
            "res-1",
        ],
    )
    assert result.exit_code == 0
    assert "Dry-run" in result.output
    assert "metadata-only" in result.output


def test_auth_login_requires_client_secrets(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--project-dir", str(tmp_path), "auth", "login"])
    assert result.exit_code == 1
    assert "client secrets" in result.output.lower()


def test_migrate_plan_help() -> None:
    result = runner.invoke(app, ["migrate", "plan", "--help"])
    assert result.exit_code == 0
    assert "destructive" in result.stdout.lower()


def test_infer_collection_help() -> None:
    result = runner.invoke(app, ["infer", "collection", "--help"])
    assert result.exit_code == 0
    assert "folder-id" in result.stdout
