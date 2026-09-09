"""Credential and Google API service construction for GDM."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, overload

from google.auth import default as google_auth_default
from google.auth.credentials import Credentials
from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build

from gdm.google_api import (
    DriveExplorerAdapter,
    ExecutingDocsService,
    ExecutingDriveService,
    ExecutingSheetsService,
    ExecutingSlidesService,
    wrap_docs_service,
    wrap_drive_service,
    wrap_sheets_service,
    wrap_slides_service,
)

logger = logging.getLogger(__name__)

# Least-privilege scopes for GDM-created or user-opened resources.
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPE_SPREADSHEETS = "https://www.googleapis.com/auth/spreadsheets"

# Broader Drive scope required to discover/adopt arbitrary existing content by path
# or name. Restricted-scope verification may be required for distributed OAuth apps.
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
SCOPE_DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"

DEFAULT_SCOPES: tuple[str, ...] = (SCOPE_DRIVE_FILE, SCOPE_SPREADSHEETS)
DISCOVERY_SCOPES: tuple[str, ...] = (SCOPE_DRIVE, SCOPE_SPREADSHEETS)

DRIVE_API_VERSION = "v3"
SHEETS_API_VERSION = "v4"
DOCS_API_VERSION = "v1"
SLIDES_API_VERSION = "v1"
TOKEN_FILENAME = "token.json"

SCOPE_ALIASES: dict[str, str] = {
    "drive": SCOPE_DRIVE,
    "drive.readonly": SCOPE_DRIVE_READONLY,
    "drive.file": SCOPE_DRIVE_FILE,
    "spreadsheets": SCOPE_SPREADSHEETS,
}


class OAuthUnavailableError(ImportError):
    """Raised when installed-app OAuth is requested without the optional extra."""


def _user_config_dir(appname: str = "gdm") -> Path:
    """Return a platform-appropriate user config directory."""
    try:
        import platformdirs

        return Path(platformdirs.user_config_dir(appname, appauthor=False))
    except ImportError:
        home = Path.home()
        xdg = home / ".config" / appname
        if xdg.parent.exists() or not (home / "Library").exists():
            return xdg
        return home / "Library" / "Application Support" / appname


def default_token_path(*, appname: str = "gdm") -> Path:
    """Default on-disk location for OAuth refresh tokens (never inside a project)."""
    return _user_config_dir(appname) / TOKEN_FILENAME


def _write_private_token(path: Path, payload: str) -> None:
    """Write an OAuth token with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            fchmod(descriptor, 0o600)
        else:  # pragma: no cover - Windows fallback
            os.chmod(path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def resolve_client_secrets_path(
    explicit: str | Path | None = None,
) -> Path | None:
    """Resolve OAuth client secrets from explicit path or ``GDM_CLIENT_SECRETS``."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("GDM_CLIENT_SECRETS")
    if env:
        return Path(env)
    return None


def _load_oauth_modules() -> tuple[Any, Any, Any]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials as UserCredentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:  # pragma: no cover - exercised via OAuthUnavailableError
        raise OAuthUnavailableError(
            "Installed-app OAuth requires the optional dependency: "
            "pip install google-drive-model[oauth]"
        ) from exc
    return Request, UserCredentials, InstalledAppFlow


def _normalize_scopes(scopes: Sequence[str] | None) -> list[str]:
    if scopes is None:
        return list(DEFAULT_SCOPES)
    resolved: list[str] = []
    for scope in scopes:
        token = scope.strip()
        if not token:
            continue
        resolved.append(SCOPE_ALIASES.get(token, token))
    return resolved


def _load_token_credentials(
    token_path: Path,
    scopes: list[str],
) -> Credentials | None:
    if not token_path.is_file():
        return None
    # Authorized-user files already record the scopes that were granted during
    # consent. Replacing them here can make refresh fail with ``invalid_scope``
    # when a broader token (for example ``drive``) is reused by a narrower
    # client path (for example ``drive.file``).
    del scopes
    _, UserCredentials, _ = _load_oauth_modules()
    creds = UserCredentials.from_authorized_user_file(str(token_path))
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        Request, _, _ = _load_oauth_modules()
        creds.refresh(Request())
        _write_private_token(token_path, creds.to_json())
        return creds
    return None


def _run_installed_app_flow(
    client_secrets: str | Path | dict[str, Any],
    scopes: list[str],
    token_path: Path,
) -> Credentials:
    _, _, InstalledAppFlow = _load_oauth_modules()
    if isinstance(client_secrets, dict):
        flow = InstalledAppFlow.from_client_config(client_secrets, scopes)
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), scopes)
    creds = flow.run_local_server(port=0)
    _write_private_token(token_path, creds.to_json())
    logger.info("Stored OAuth token at %s", token_path)
    return creds


def build_credentials(
    *,
    credentials: Credentials | None = None,
    use_adc: bool = False,
    service_account_file: str | Path | None = None,
    service_account_info: dict[str, Any] | None = None,
    oauth_client_secrets: str | Path | dict[str, Any] | None = None,
    token_path: str | Path | None = None,
    scopes: Sequence[str] | None = None,
    allow_oauth_interactive: bool = False,
) -> Credentials:
    """Build Google credentials from explicit inputs, ADC, or optional OAuth.

    Precedence: injected ``credentials`` > service account > on-disk OAuth token
    > interactive OAuth (when enabled) > Application Default Credentials.
    """
    normalized = _normalize_scopes(scopes)
    if credentials is not None:
        return credentials

    if service_account_file is not None:
        return service_account.Credentials.from_service_account_file(
            str(service_account_file),
            scopes=normalized,
        )
    if service_account_info is not None:
        return service_account.Credentials.from_service_account_info(
            service_account_info,
            scopes=normalized,
        )

    resolved_token = (
        Path(token_path) if token_path is not None else default_token_path()
    )
    token_creds = _load_token_credentials(resolved_token, normalized)
    if token_creds is not None:
        has_scopes = getattr(token_creds, "has_scopes", None)
        scopes_satisfied = (
            not normalized or not callable(has_scopes) or bool(has_scopes(normalized))
        )
        if scopes_satisfied:
            return token_creds
        if not allow_oauth_interactive:
            msg = (
                f"Stored OAuth token {resolved_token} does not grant all required "
                "scopes. Run `gdm auth login` to authorize the current scope set."
            )
            raise PermissionDeniedError(msg)

    resolved_secrets = resolve_client_secrets_path(
        oauth_client_secrets if isinstance(oauth_client_secrets, (str, Path)) else None
    )
    secrets = oauth_client_secrets
    if secrets is None and resolved_secrets is not None:
        secrets = resolved_secrets

    if secrets is not None and allow_oauth_interactive:
        return _run_installed_app_flow(secrets, normalized, resolved_token)

    if use_adc or (
        service_account_file is None
        and service_account_info is None
        and secrets is None
    ):
        adc_creds, _ = google_auth_default(scopes=normalized)
        return adc_creds

    if secrets is not None:
        raise OAuthUnavailableError(
            "OAuth client secrets were provided but no token exists. "
            "Pass allow_oauth_interactive=True to run the installed-app flow, "
            "or install google-drive-model[oauth]."
        )

    msg = "No credential source configured."
    raise ValueError(msg)


def login_interactive(
    *,
    scopes: Sequence[str] | None = None,
    client_secrets: str | Path | dict[str, Any] | None = None,
    token_path: str | Path | None = None,
) -> Credentials:
    """Run installed-app OAuth and persist the refresh token locally."""
    resolved_secrets = client_secrets or resolve_client_secrets_path()
    if resolved_secrets is None:
        msg = (
            "OAuth client secrets are required. Pass client_secrets= or set "
            "GDM_CLIENT_SECRETS to a client_secret JSON path."
        )
        raise ValueError(msg)
    requested_scopes = list(scopes) if scopes is not None else list(DISCOVERY_SCOPES)
    return build_credentials(
        oauth_client_secrets=resolved_secrets,
        token_path=token_path,
        scopes=requested_scopes,
        allow_oauth_interactive=True,
    )


@overload
def build_drive_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: Literal[True] = True,
    supports_all_drives: bool = True,
) -> ExecutingDriveService: ...


@overload
def build_drive_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: Literal[False],
    supports_all_drives: bool = True,
) -> Resource: ...


def build_drive_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: bool = True,
    supports_all_drives: bool = True,
) -> ExecutingDriveService | Resource:
    """Construct a Drive v3 discovery client."""
    raw = build(
        "drive",
        DRIVE_API_VERSION,
        credentials=credentials,
        cache_discovery=cache_discovery,
    )
    if wrap:
        return wrap_drive_service(raw, supports_all_drives=supports_all_drives)
    return raw


@overload
def build_sheets_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: Literal[True] = True,
) -> ExecutingSheetsService: ...


@overload
def build_sheets_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: Literal[False],
) -> Resource: ...


def build_sheets_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: bool = True,
) -> ExecutingSheetsService | Resource:
    """Construct a Sheets v4 discovery client."""
    raw = build(
        "sheets",
        SHEETS_API_VERSION,
        credentials=credentials,
        cache_discovery=cache_discovery,
    )
    if wrap:
        return wrap_sheets_service(raw)
    return raw


@overload
def build_docs_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: Literal[True] = True,
) -> ExecutingDocsService: ...


@overload
def build_docs_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: Literal[False],
) -> Resource: ...


def build_docs_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: bool = True,
) -> ExecutingDocsService | Resource:
    """Construct a Google Docs v1 discovery client."""
    raw = build(
        "docs",
        DOCS_API_VERSION,
        credentials=credentials,
        cache_discovery=cache_discovery,
    )
    if wrap:
        return wrap_docs_service(raw)
    return raw


@overload
def build_slides_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: Literal[True] = True,
) -> ExecutingSlidesService: ...


@overload
def build_slides_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: Literal[False],
) -> Resource: ...


def build_slides_service(
    credentials: Credentials,
    *,
    cache_discovery: bool = False,
    wrap: bool = True,
) -> ExecutingSlidesService | Resource:
    """Construct a Google Slides v1 discovery client."""
    raw = build(
        "slides",
        SLIDES_API_VERSION,
        credentials=credentials,
        cache_discovery=cache_discovery,
    )
    if wrap:
        return wrap_slides_service(raw)
    return raw


def build_explorer_service(
    *,
    credentials: Credentials | None = None,
    use_adc: bool = True,
    token_path: str | Path | None = None,
    scopes: Sequence[str] | None = None,
) -> DriveExplorerAdapter:
    """Build a read-only Drive explorer adapter using token, ADC, or OAuth."""
    creds = build_credentials(
        credentials=credentials,
        use_adc=use_adc,
        token_path=token_path,
        scopes=scopes or [SCOPE_DRIVE_READONLY, SCOPE_SPREADSHEETS],
    )
    drive = build_drive_service(creds, wrap=True)
    return DriveExplorerAdapter(drive)


def save_token(credentials: Credentials, token_path: str | Path | None = None) -> Path:
    """Persist authorized-user credentials to disk without logging secrets."""
    to_json = getattr(credentials, "to_json", None)
    if not callable(to_json):
        msg = "Only authorized-user OAuth credentials can be saved as tokens."
        raise TypeError(msg)
    path = Path(token_path) if token_path is not None else default_token_path()
    _write_private_token(path, to_json())
    logger.info("Saved OAuth token at %s", path)
    return path


def scopes_for_discovery() -> list[str]:
    """Return scopes needed for arbitrary Drive discovery/adoption."""
    return list(DISCOVERY_SCOPES)


def scopes_for_created_resources() -> list[str]:
    """Return least-privilege scopes for GDM-managed resources."""
    return list(DEFAULT_SCOPES)


def explain_scope_requirements() -> str:
    """Human-readable scope guidance for operators."""
    return (
        "Use drive.file + spreadsheets for resources GDM creates or the user "
        "opens through the app. Discovering or adopting arbitrary existing Drive "
        "content by path requires the broader drive scope (or drive.readonly for "
        "read-only adoption), which may trigger Google's restricted-scope review "
        "for distributed OAuth clients."
    )


# Backward-compatible re-exports for modules that imported transport types from auth.
from gdm.errors import (  # noqa: E402
    AmbiguousResourceError,
    ForeignProjectError,
    MappingMode,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedOperationError,
)
from gdm.retry import RetryPolicy, execute_with_retry  # noqa: E402

__all__ = [
    "DEFAULT_SCOPES",
    "DISCOVERY_SCOPES",
    "DOCS_API_VERSION",
    "SCOPE_DRIVE",
    "SCOPE_DRIVE_FILE",
    "SCOPE_DRIVE_READONLY",
    "SCOPE_SPREADSHEETS",
    "SLIDES_API_VERSION",
    "AmbiguousResourceError",
    "ForeignProjectError",
    "MappingMode",
    "NotFoundError",
    "OAuthUnavailableError",
    "PermissionDeniedError",
    "RetryPolicy",
    "UnsupportedOperationError",
    "build_credentials",
    "build_docs_service",
    "build_drive_service",
    "build_explorer_service",
    "build_sheets_service",
    "build_slides_service",
    "default_token_path",
    "execute_with_retry",
    "explain_scope_requirements",
    "login_interactive",
    "resolve_client_secrets_path",
    "save_token",
    "scopes_for_created_resources",
    "scopes_for_discovery",
]
