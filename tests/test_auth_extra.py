"""Additional auth module tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gdm.auth import (
    build_explorer_service,
    login_interactive,
    resolve_client_secrets_path,
)


def test_resolve_client_secrets_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GDM_CLIENT_SECRETS", "/tmp/client.json")
    assert resolve_client_secrets_path() == Path("/tmp/client.json")


def test_login_interactive_requires_secrets() -> None:
    with pytest.raises(ValueError, match="client secrets"):
        login_interactive()


def test_login_interactive_runs_flow(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    secrets.write_text('{"installed": {}}', encoding="utf-8")
    mock_creds = MagicMock()
    with patch("gdm.auth.build_credentials", return_value=mock_creds) as build:
        result = login_interactive(client_secrets=secrets, scopes=["drive.readonly"])
    assert result is mock_creds
    build.assert_called_once()


def test_build_explorer_service_uses_credentials() -> None:
    from gdm.google_api import ExecutingDriveService

    creds = MagicMock()
    drive_service = ExecutingDriveService(MagicMock())
    with patch("gdm.auth.build_credentials", return_value=creds):
        with patch("gdm.auth.build_drive_service", return_value=drive_service):
            adapter = build_explorer_service(credentials=creds, use_adc=False)
    assert isinstance(adapter._service, ExecutingDriveService)
