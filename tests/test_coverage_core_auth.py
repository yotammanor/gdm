"""Branch coverage for gdm.auth."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from gdm.auth import (
    OAuthUnavailableError,
    _load_token_credentials,
    _normalize_scopes,
    _run_installed_app_flow,
    _user_config_dir,
    build_credentials,
    build_explorer_service,
    default_token_path,
    login_interactive,
    resolve_client_secrets_path,
    save_token,
)
from gdm.errors import PermissionDeniedError


def test_user_config_dir_without_platformdirs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delitem(sys.modules, "platformdirs", raising=False)
    fake_platformdirs = ModuleType("platformdirs")

    def _missing(*_args: object, **_kwargs: object) -> None:
        raise ImportError("no platformdirs")

    fake_platformdirs.user_config_dir = _missing  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "platformdirs", fake_platformdirs)
    path = _user_config_dir()
    assert path == Path.home() / ".config" / "gdm"


def test_user_config_dir_macos_library_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delitem(sys.modules, "platformdirs", raising=False)
    fake_platformdirs = ModuleType("platformdirs")

    def _missing(*_args: object, **_kwargs: object) -> None:
        raise ImportError("no platformdirs")

    fake_platformdirs.user_config_dir = _missing  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "platformdirs", fake_platformdirs)
    home = tmp_path / "home"
    library = home / "Library"
    library.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    path = _user_config_dir()
    assert path == library / "Application Support" / "gdm"


def test_normalize_scopes_aliases_and_empty_tokens() -> None:
    scopes = _normalize_scopes([" drive ", "", "drive.file", "spreadsheets"])
    assert "https://www.googleapis.com/auth/drive.file" in scopes
    assert "https://www.googleapis.com/auth/spreadsheets" in scopes
    assert "" not in scopes
    assert _normalize_scopes(None) != []
    assert "drive.file" in _normalize_scopes(None)[0]


def test_resolve_client_secrets_path_explicit_and_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "client.json"
    assert resolve_client_secrets_path(explicit) == explicit
    assert resolve_client_secrets_path() is None
    env_path = tmp_path / "from-env.json"
    monkeypatch.setenv("GDM_CLIENT_SECRETS", str(env_path))
    assert resolve_client_secrets_path() == env_path


def test_load_token_credentials_missing_file(tmp_path: Path) -> None:
    assert _load_token_credentials(tmp_path / "missing.json", []) is None


def test_load_token_credentials_refreshes_expired_token(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    mock_request = MagicMock()
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "refresh"
    mock_creds.to_json.return_value = '{"refreshed": true}'
    with patch("gdm.auth._load_oauth_modules") as load:
        request_cls, user_cls, _ = load.return_value = (
            mock_request,
            MagicMock(),
            MagicMock(),
        )
        user_cls.from_authorized_user_file.return_value = mock_creds
        result = _load_token_credentials(token_path, ["scope"])
    user_cls.from_authorized_user_file.assert_called_once_with(str(token_path))
    mock_creds.refresh.assert_called_once_with(request_cls.return_value)
    assert result is mock_creds
    assert token_path.read_text(encoding="utf-8") == '{"refreshed": true}'


def test_load_token_credentials_invalid_without_refresh(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = None
    with patch("gdm.auth._load_oauth_modules") as load:
        _, user_cls, _ = load.return_value = (MagicMock(), MagicMock(), MagicMock())
        user_cls.from_authorized_user_file.return_value = mock_creds
        assert _load_token_credentials(token_path, []) is None


def test_run_installed_app_flow_from_dict(tmp_path: Path) -> None:
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = "{}"
    token_path = tmp_path / "token.json"
    with patch("gdm.auth._load_oauth_modules") as load:
        _, _, flow_cls = load.return_value = (MagicMock(), MagicMock(), MagicMock())
        flow = flow_cls.from_client_config.return_value
        flow.run_local_server.return_value = mock_creds
        result = _run_installed_app_flow({"installed": {}}, ["scope"], token_path)
    flow_cls.from_client_config.assert_called_once()
    assert result is mock_creds
    assert token_path.is_file()


def test_run_installed_app_flow_from_file(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    secrets.write_text("{}", encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.to_json.return_value = "{}"
    token_path = tmp_path / "token.json"
    with patch("gdm.auth._load_oauth_modules") as load:
        _, _, flow_cls = load.return_value = (MagicMock(), MagicMock(), MagicMock())
        flow = flow_cls.from_client_secrets_file.return_value
        flow.run_local_server.return_value = mock_creds
        result = _run_installed_app_flow(secrets, ["scope"], token_path)
    flow_cls.from_client_secrets_file.assert_called_once()
    assert result is mock_creds


def test_build_credentials_interactive_oauth(tmp_path: Path) -> None:
    mock_creds = MagicMock()
    with patch("gdm.auth._run_installed_app_flow", return_value=mock_creds) as run:
        result = build_credentials(
            oauth_client_secrets={"installed": {}},
            token_path=tmp_path / "token.json",
            allow_oauth_interactive=True,
        )
    run.assert_called_once()
    assert result is mock_creds


def test_build_credentials_default_adc_when_no_sources(tmp_path: Path) -> None:
    mock_creds = MagicMock()
    with patch("gdm.auth.google_auth_default", return_value=(mock_creds, "proj")):
        assert (
            build_credentials(token_path=tmp_path / "missing-token.json") is mock_creds
        )


def test_build_credentials_no_source_still_uses_adc_by_default() -> None:
    mock_creds = MagicMock()
    with (
        patch("gdm.auth._load_token_credentials", return_value=None),
        patch("gdm.auth.resolve_client_secrets_path", return_value=None),
        patch("gdm.auth.google_auth_default", return_value=(mock_creds, "proj")) as adc,
    ):
        assert build_credentials(use_adc=False) is mock_creds
    adc.assert_called_once()


def test_login_interactive_requires_secrets() -> None:
    with (
        patch("gdm.auth.resolve_client_secrets_path", return_value=None),
        pytest.raises(ValueError, match="client secrets"),
    ):
        login_interactive()


def test_login_interactive_delegates_to_build_credentials(tmp_path: Path) -> None:
    secrets = tmp_path / "client.json"
    mock_creds = MagicMock()
    with patch("gdm.auth.build_credentials", return_value=mock_creds) as build:
        result = login_interactive(
            client_secrets=secrets, token_path=tmp_path / "t.json"
        )
    build.assert_called_once()
    assert result is mock_creds


def test_build_explorer_service_wraps_drive() -> None:
    from gdm.google_api import ExecutingDriveService

    mock_creds = MagicMock()
    mock_drive = MagicMock(spec=ExecutingDriveService)
    with (
        patch("gdm.auth.build_credentials", return_value=mock_creds),
        patch("gdm.auth.build_drive_service", return_value=mock_drive),
        patch("gdm.auth.DriveExplorerAdapter") as adapter_cls,
    ):
        adapter_cls.return_value = MagicMock()
        build_explorer_service(use_adc=True)
    adapter_cls.assert_called_once_with(mock_drive)


def test_build_credentials_oauth_dict_secrets_without_interactive(
    tmp_path: Path,
) -> None:
    with (
        patch("gdm.auth._load_token_credentials", return_value=None),
        pytest.raises(OAuthUnavailableError, match="allow_oauth_interactive"),
    ):
        build_credentials(
            oauth_client_secrets={"installed": {}},
            token_path=tmp_path / "token.json",
        )


def test_build_credentials_from_valid_token_file(tmp_path: Path) -> None:
    token = {
        "token": "abc",
        "refresh_token": "refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
        "scopes": ["https://www.googleapis.com/auth/drive.file"],
    }
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps(token), encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = True
    with patch("gdm.auth._load_oauth_modules") as load:
        _, user_cls, _ = load.return_value = (MagicMock(), MagicMock(), MagicMock())
        user_cls.from_authorized_user_file.return_value = mock_creds
        assert build_credentials(token_path=token_path) is mock_creds


def test_build_credentials_rejects_token_missing_required_scopes(
    tmp_path: Path,
) -> None:
    token = MagicMock()
    token.has_scopes.return_value = False
    token_path = tmp_path / "token.json"
    with (
        patch("gdm.auth._load_token_credentials", return_value=token),
        pytest.raises(PermissionDeniedError, match="does not grant"),
    ):
        build_credentials(token_path=token_path, scopes=["required"])


def test_interactive_oauth_reauthorizes_token_with_insufficient_scopes(
    tmp_path: Path,
) -> None:
    token = MagicMock()
    token.has_scopes.return_value = False
    replacement = MagicMock()
    secrets = {"installed": {}}
    with (
        patch("gdm.auth._load_token_credentials", return_value=token),
        patch("gdm.auth._run_installed_app_flow", return_value=replacement) as flow,
    ):
        result = build_credentials(
            oauth_client_secrets=secrets,
            token_path=tmp_path / "token.json",
            scopes=["required"],
            allow_oauth_interactive=True,
        )
    assert result is replacement
    flow.assert_called_once()


def test_default_token_path_uses_user_config_dir() -> None:
    with patch("gdm.auth._user_config_dir", return_value=Path("/cfg/gdm")):
        assert default_token_path() == Path("/cfg/gdm/token.json")


def test_save_token_persists_json(tmp_path: Path) -> None:
    creds = MagicMock()
    creds.to_json.return_value = '{"saved": true}'
    path = save_token(creds, tmp_path / "token.json")
    assert path.read_text(encoding="utf-8") == '{"saved": true}'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
