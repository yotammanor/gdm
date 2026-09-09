"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Typer forces a colour terminal whenever GITHUB_ACTIONS/FORCE_COLOR/PY_COLORS is
# set, so CLI output carries ANSI escapes on CI but not locally, and rich wraps
# help text to the ambient width. Both make plain substring assertions on command
# output pass locally and fail on CI. Pin the behaviour for every test.
TEST_TERMINAL_WIDTH = "200"
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ["TERMINAL_WIDTH"] = TEST_TERMINAL_WIDTH
os.environ["NO_COLOR"] = "1"


def pytest_configure() -> None:
    """Pin typer's terminal settings for in-process CliRunner invocations.

    The environment variables above cover subprocesses, but typer reads them
    once at import time, so patch the resolved constants too rather than
    depending on this module being imported before typer.
    """
    from typer import rich_utils

    rich_utils.FORCE_TERMINAL = False
    rich_utils.MAX_WIDTH = int(TEST_TERMINAL_WIDTH)
