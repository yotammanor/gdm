"""Shared constants for collection storage."""

from __future__ import annotations

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
DOC_MIME = "application/vnd.google-apps.document"
SLIDE_MIME = "application/vnd.google-apps.presentation"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
JSON_MIME = "application/json"

NATIVE_WORKSPACE_MIMES = frozenset({DOC_MIME, SLIDE_MIME, SHEET_MIME})

READ_ONLY_DRIVE_FIELDS = frozenset(
    {
        "id",
        "mimeType",
        "createdTime",
        "modifiedTime",
        "webViewLink",
        "size",
        "md5Checksum",
        "capabilities",
        "shortcutDetails",
        "resourceKey",
        "trashed",
        "version",
    }
)

WRITABLE_DRIVE_FIELDS = frozenset({"name", "description", "starred", "parents"})

METADATA_ID_COLUMN = "_gdm_file_id"
METADATA_VERSION_COLUMN = "_gdm_version"

METADATA_JSON_FILENAME = "metadata.json"
METADATA_SIDECAR_FORMAT_VERSION = "1"
METADATA_BACKEND_SHEETS = "sheets"
METADATA_BACKEND_JSON = "json"

MAX_DRIVE_PROPERTIES_PER_FILE = 30
MAX_DRIVE_PROPERTY_BYTES = 124

DEFAULT_LIST_FIELDS = (
    "id,name,mimeType,version,md5Checksum,properties,appProperties,parents,trashed,"
    "description,starred,createdTime,modifiedTime,webViewLink,size,"
    "capabilities,shortcutDetails,resourceKey"
)

NATIVE_CREATE_MIMES: dict[str, str] = {
    "document": DOC_MIME,
    "doc": DOC_MIME,
    "slide": SLIDE_MIME,
    "slides": SLIDE_MIME,
    "presentation": SLIDE_MIME,
    "spreadsheet": SHEET_MIME,
    "sheet": SHEET_MIME,
    "folder": FOLDER_MIME,
}
