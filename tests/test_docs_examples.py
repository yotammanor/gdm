"""Validate documentation examples and generated demo assets."""

from __future__ import annotations

import compileall
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_SRC = REPO_ROOT / "docs_src"
DEMO_CAST = REPO_ROOT / "docs" / "assets" / "demo.cast"
DEMO_GIF = REPO_ROOT / "docs" / "assets" / "demo.gif"

RUNNABLE_WITH_FAKE = (
    "quickstart_collection.py",
    "quickstart_session.py",
    "query_basic.py",
    "query_sheet.py",
    "crud_create.py",
)


def test_docs_src_compiles() -> None:
    assert compileall.compile_dir(str(DOCS_SRC), quiet=1)


@pytest.mark.parametrize("script_name", RUNNABLE_WITH_FAKE)
def test_docs_src_runs_with_fake_backend(script_name: str) -> None:
    env = os.environ.copy()
    env["GDM_USE_FAKE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(DOCS_SRC), str(REPO_ROOT / "src")])
    completed = subprocess.run(
        [sys.executable, str(DOCS_SRC / script_name)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_demo_cast_has_no_stale_source_name() -> None:
    text = DEMO_CAST.read_text(encoding="utf-8")
    assert "source_name" not in text


def test_demo_gif_is_committed_animation() -> None:
    data = DEMO_GIF.read_bytes()
    assert data.startswith((b"GIF87a", b"GIF89a"))
    # Each frame is introduced by a Graphic Control Extension block, so more
    # than one means a real recording rather than a single-frame placeholder.
    assert data.count(b"\x21\xf9\x04") > 1


def test_infer_file_cli_emits_public_field_targets() -> None:
    sample = REPO_ROOT / "docs" / "assets" / "demo-sample.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gdm.cli",
            "infer",
            "file",
            str(sample),
            "--model-name",
            "SampleDoc",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert "source_name" not in completed.stdout
    assert 'storage="file"' in completed.stdout
    assert 'codec="json"' in completed.stdout
