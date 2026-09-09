"""Tests for gdm.auth."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gdm.auth import (
    DEFAULT_SCOPES,
    DISCOVERY_SCOPES,
    OAuthUnavailableError,
    build_credentials,
    build_docs_service,
    build_drive_service,
    build_sheets_service,
    build_slides_service,
    default_token_path,
    explain_scope_requirements,
    save_token,
    scopes_for_created_resources,
    scopes_for_discovery,
)


def test_default_scopes_are_least_privilege() -> None:
    assert "drive.file" in DEFAULT_SCOPES[0]
    assert "spreadsheets" in DEFAULT_SCOPES[1]
    assert "drive.file" not in DISCOVERY_SCOPES[0]


def test_explain_scope_requirements_mentions_broad_drive() -> None:
    text = explain_scope_requirements()
    assert "drive.file" in text
    assert "broader drive scope" in text


def test_scopes_helpers() -> None:
    assert scopes_for_discovery() == list(DISCOVERY_SCOPES)
    assert scopes_for_created_resources() == list(DEFAULT_SCOPES)


def test_build_credentials_injected() -> None:
    creds = MagicMock()
    assert build_credentials(credentials=creds) is creds


def test_build_credentials_service_account_file(tmp_path: Path) -> None:
    path = tmp_path / "sa.json"
    path.write_text("{}", encoding="utf-8")
    mock_creds = MagicMock()
    with patch(
        "gdm.auth.service_account.Credentials.from_service_account_file",
        return_value=mock_creds,
    ) as from_file:
        creds = build_credentials(service_account_file=path)
    from_file.assert_called_once()
    assert creds is mock_creds


def test_build_credentials_service_account_info() -> None:
    info = {"type": "service_account", "client_email": "a@b.iam.gserviceaccount.com"}
    mock_creds = MagicMock()
    with patch(
        "gdm.auth.service_account.Credentials.from_service_account_info",
        return_value=mock_creds,
    ) as from_info:
        creds = build_credentials(service_account_info=info)
    from_info.assert_called_once()
    assert creds is mock_creds


def test_build_credentials_adc(tmp_path: Path) -> None:
    mock_creds = MagicMock()
    with patch("gdm.auth.google_auth_default", return_value=(mock_creds, "project")):
        assert (
            build_credentials(
                use_adc=True,
                token_path=tmp_path / "missing-token.json",
            )
            is mock_creds
        )


def test_build_credentials_from_token_file(tmp_path: Path) -> None:
    token = {
        "token": "abc",
        "refresh_token": "refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
        "scopes": list(DEFAULT_SCOPES),
    }
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps(token), encoding="utf-8")

    mock_creds = MagicMock()
    mock_creds.valid = True
    with patch("gdm.auth._load_oauth_modules") as load:
        _, user_cls, _ = load.return_value = (MagicMock(), MagicMock(), MagicMock())
        user_cls.from_authorized_user_file.return_value = mock_creds
        result = build_credentials(token_path=token_path)
    assert result is mock_creds


def test_oauth_unavailable_without_extra() -> None:
    with patch.dict("sys.modules", {"google_auth_oauthlib.flow": None}):
        with pytest.raises(
            OAuthUnavailableError, match="google-drive-model\\[oauth\\]"
        ):
            build_credentials(
                oauth_client_secrets={"installed": {}}, allow_oauth_interactive=True
            )


def test_oauth_secrets_without_token_raises_helpful_error() -> None:
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = False
    with patch("gdm.auth._load_oauth_modules") as load:
        _, user_cls, _ = load.return_value = (MagicMock(), MagicMock(), MagicMock())
        user_cls.from_authorized_user_file.return_value = None
        with pytest.raises(OAuthUnavailableError, match="allow_oauth_interactive"):
            build_credentials(oauth_client_secrets=Path("client.json"))


def test_default_token_path_under_config() -> None:
    path = default_token_path()
    assert path.name == "token.json"
    assert "gdm" in str(path)


def test_build_services() -> None:
    creds = MagicMock()
    with patch("gdm.auth.build") as build:
        build.return_value = MagicMock()
        drive = build_drive_service(creds, wrap=False)
        sheets = build_sheets_service(creds, wrap=False)
        docs = build_docs_service(creds, wrap=False)
        slides = build_slides_service(creds, wrap=False)
        assert drive is build.return_value
        assert sheets is build.return_value
        assert docs is build.return_value
        assert slides is build.return_value
        wrapped_drive = build_drive_service(creds, wrap=True)
        assert wrapped_drive.__class__.__name__ == "ExecutingDriveService"
        wrapped_docs = build_docs_service(creds, wrap=True)
        assert wrapped_docs.__class__.__name__ == "ExecutingDocsService"
        wrapped_slides = build_slides_service(creds, wrap=True)
        assert wrapped_slides.__class__.__name__ == "ExecutingSlidesService"
        assert build.call_count == 7


def test_build_credentials_refreshes_expired_token_preserving_scopes(
    tmp_path: Path,
) -> None:
    token = {
        "token": "abc",
        "refresh_token": "refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "cid",
        "client_secret": "secret",
        "scopes": [
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    }
    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps(token), encoding="utf-8")

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "refresh"
    mock_creds.scopes = list(token["scopes"])
    mock_creds.to_json.return_value = json.dumps(token)

    with patch("gdm.auth._load_oauth_modules") as load:
        request_cls, user_cls, _ = load.return_value = (
            MagicMock(),
            MagicMock(),
            MagicMock(),
        )
        user_cls.from_authorized_user_file.return_value = mock_creds
        result = build_credentials(
            token_path=token_path,
            scopes=["drive.file", "spreadsheets"],
        )
    mock_creds.refresh.assert_called_once()
    request_cls.assert_called()
    assert result is mock_creds
    assert mock_creds.scopes == token["scopes"]


def test_save_token_requires_user_credentials(tmp_path: Path) -> None:
    bad = MagicMock(spec=["refresh"])
    with pytest.raises(TypeError, match="authorized-user"):
        save_token(bad, tmp_path / "token.json")
    creds = MagicMock()
    creds.to_json.return_value = "{}"
    path = save_token(creds, tmp_path / "token.json")
    assert path.read_text(encoding="utf-8") == "{}"
