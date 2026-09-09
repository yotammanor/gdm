"""Drive v3 backend for one-model-per-file storage."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from gdm.codecs import JSON_MIME, TEXT_MIME
from gdm.errors import (
    AmbiguousResourceError,
    ConflictError,
    NotFoundError,
    UnsupportedOperationError,
)
from gdm.retry import RetryPolicy, execute_with_retry, normalize_http_error

logger = logging.getLogger(__name__)

NATIVE_EXPORT_MIMES: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

NATIVE_RICH_MIMES = frozenset(NATIVE_EXPORT_MIMES)


def escape_drive_query_value(value: str) -> str:
    """Escape a value for use inside Drive ``q`` string literals."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_app_properties(
    *,
    project_id: str,
    model_name: str,
    record_id: str,
    schema_version: str | None = None,
) -> dict[str, str]:
    props = {
        "gdm_project": project_id,
        "gdm_model": model_name,
        "gdm_id": record_id,
    }
    if schema_version is not None:
        props["gdm_schema"] = schema_version
    return props


@runtime_checkable
class DriveMediaClient(Protocol):
    def get_media(self, *, fileId: str, supportsAllDrives: bool = True) -> Any: ...

    def create(
        self,
        *,
        fileId: str | None = None,
        body: dict[str, Any] | None = None,
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]: ...

    def update(
        self,
        *,
        fileId: str,
        body: dict[str, Any] | None = None,
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]: ...


@runtime_checkable
class DriveFilesBackendClient(Protocol):
    def get(
        self,
        *,
        fileId: str,
        fields: str,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]: ...

    def list(
        self,
        *,
        q: str | None = None,
        fields: str | None = None,
        pageSize: int | None = None,
        pageToken: str | None = None,
        supportsAllDrives: bool = True,
        includeItemsFromAllDrives: bool = True,
        corpora: str | None = None,
        driveId: str | None = None,
    ) -> dict[str, Any]: ...

    def create(
        self,
        *,
        body: dict[str, Any],
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
    ) -> dict[str, Any]: ...

    def update(
        self,
        *,
        fileId: str,
        body: dict[str, Any] | None = None,
        media_body: Any = None,
        fields: str | None = None,
        supportsAllDrives: bool = True,
        addParents: str | None = None,
        removeParents: str | None = None,
    ) -> dict[str, Any]: ...

    def delete(self, *, fileId: str, supportsAllDrives: bool = True) -> None: ...

    def export(
        self,
        *,
        fileId: str,
        mimeType: str,
    ) -> bytes: ...

    def get_media(self, *, fileId: str, supportsAllDrives: bool = True) -> Any: ...


@runtime_checkable
class DriveServiceLike(Protocol):
    @property
    def files(self) -> DriveFilesBackendClient: ...


@dataclass
class DriveRecordMeta:
    file_id: str
    name: str
    mime_type: str
    version: str | None
    md5_checksum: str | None
    app_properties: dict[str, str]
    parents: list[str] = field(default_factory=list)


@dataclass
class DriveScanResult:
    items: list[DriveRecordMeta]
    next_page_token: str | None = None
    truncated: bool = False


class DriveBackend:
    """Synchronous Drive adapter for structured file-backed models."""

    DEFAULT_FIELDS = (
        "id,name,mimeType,version,md5Checksum,appProperties,parents,trashed"
    )

    def __init__(
        self,
        service: DriveServiceLike,
        *,
        project_id: str,
        retry_policy: RetryPolicy | None = None,
        supports_all_drives: bool = True,
        shared_drive_id: str | None = None,
    ) -> None:
        self.service = service
        self.project_id = project_id
        self.retry_policy = retry_policy or RetryPolicy()
        self.supports_all_drives = supports_all_drives
        self.shared_drive_id = shared_drive_id

    def _list_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "supportsAllDrives": self.supports_all_drives,
            "includeItemsFromAllDrives": self.supports_all_drives,
        }
        if self.shared_drive_id:
            kwargs["corpora"] = "drive"
            kwargs["driveId"] = self.shared_drive_id
        return kwargs

    def _call(
        self,
        func: Callable[[], Any],
        *,
        retryable: bool = True,
    ) -> Any:
        if getattr(self.service, "_gdm_executes_with_retry", False):
            return func()
        return execute_with_retry(
            func,
            policy=self.retry_policy,
            retryable=retryable,
        )

    def _normalize_http_error(self, exc: HttpError) -> Exception:
        return normalize_http_error(exc)

    def get_metadata(
        self, file_id: str, *, fields: str | None = None
    ) -> DriveRecordMeta:
        def _get() -> dict[str, Any]:
            try:
                return self.service.files.get(
                    fileId=file_id,
                    fields=fields or self.DEFAULT_FIELDS,
                    supportsAllDrives=self.supports_all_drives,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        data = self._call(_get)
        return self._meta_from_file(data)

    def read_json(
        self,
        file_id: str,
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        meta = self.get_metadata(file_id)
        self._check_conflict(meta, expected_version, expected_fingerprint)
        raw = self._download_bytes(meta)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            msg = f"Expected JSON object in file {file_id}."
            raise UnsupportedOperationError(msg)
        return payload

    def write_json(
        self,
        *,
        parent_id: str,
        model_name: str,
        record_id: str,
        payload: dict[str, Any],
        file_id: str | None = None,
        name: str | None = None,
        schema_version: str | None = None,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
        preserve_unknown: bool = True,
    ) -> DriveRecordMeta:
        existing_app: dict[str, str] | None = None
        if file_id is not None:
            meta = self.get_metadata(file_id)
            self._check_conflict(meta, expected_version, expected_fingerprint)
            existing_app = meta.app_properties
            if meta.mime_type in NATIVE_RICH_MIMES:
                msg = (
                    f"Native Workspace file {file_id} ({meta.mime_type}) "
                    "cannot be updated as structured JSON."
                )
                raise UnsupportedOperationError(msg)
            if preserve_unknown and meta.mime_type == JSON_MIME:
                existing = self.read_json(file_id)
                merged = {**existing, **payload}
                payload = merged

        body: dict[str, Any] = {
            "mimeType": JSON_MIME,
            "appProperties": self._merge_routing_app_properties(
                existing=existing_app,
                model_name=model_name,
                record_id=record_id,
                schema_version=schema_version,
            ),
        }
        if name is not None:
            body["name"] = name

        media = MediaIoBaseUpload(
            io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
            mimetype=JSON_MIME,
            resumable=False,
        )

        def _write() -> dict[str, Any]:
            try:
                if file_id is None:
                    body["parents"] = [parent_id]
                    return self.service.files.create(
                        body=body,
                        media_body=media,
                        fields=self.DEFAULT_FIELDS,
                        supportsAllDrives=self.supports_all_drives,
                    )
                return self.service.files.update(
                    fileId=file_id,
                    body=body,
                    media_body=media,
                    fields=self.DEFAULT_FIELDS,
                    supportsAllDrives=self.supports_all_drives,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        data = self._call(_write, retryable=False)
        return self._meta_from_file(data)

    def delete(
        self,
        file_id: str,
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> None:
        if expected_version is not None or expected_fingerprint is not None:
            meta = self.get_metadata(file_id)
            self._check_conflict(meta, expected_version, expected_fingerprint)

        def _delete() -> None:
            try:
                self.service.files.delete(
                    fileId=file_id,
                    supportsAllDrives=self.supports_all_drives,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        self._call(_delete, retryable=False)

    def scan_by_model(
        self,
        *,
        model_name: str,
        parent_id: str | None = None,
        page_size: int = 100,
        page_token: str | None = None,
        scan_limit: int = 1000,
    ) -> DriveScanResult:
        escaped_project = escape_drive_query_value(self.project_id)
        project_clause = (
            f"appProperties has {{ key='gdm_project' and value='{escaped_project}' }}"
        )
        model_clause = (
            "appProperties has "
            f"{{ key='gdm_model' and value='{escape_drive_query_value(model_name)}' }}"
        )
        clauses = [project_clause, model_clause, "trashed = false"]
        if parent_id is not None:
            clauses.append(f"'{parent_id}' in parents")
        query = " and ".join(clauses)

        collected: list[DriveRecordMeta] = []
        token = page_token
        truncated = False
        while len(collected) < scan_limit:
            batch_size = min(page_size, scan_limit - len(collected))

            def _list(
                page_token: str | None = token,
                size: int = batch_size,
            ) -> dict[str, Any]:
                try:
                    return self.service.files.list(
                        q=query,
                        fields=f"nextPageToken,files({self.DEFAULT_FIELDS})",
                        pageSize=size,
                        pageToken=page_token,
                        **self._list_kwargs(),
                    )
                except HttpError as exc:
                    raise self._normalize_http_error(exc) from exc

            response = self._call(_list)
            files = response.get("files", [])
            collected.extend(self._meta_from_file(f) for f in files)
            token = response.get("nextPageToken")
            if not token:
                return DriveScanResult(
                    items=collected, next_page_token=None, truncated=False
                )
            if len(collected) >= scan_limit:
                truncated = True
                return DriveScanResult(
                    items=collected[:scan_limit],
                    next_page_token=token,
                    truncated=truncated,
                )
        return DriveScanResult(
            items=collected, next_page_token=token, truncated=truncated
        )

    def find_by_name(
        self,
        *,
        name: str,
        parent_id: str,
        mime_type: str | None = None,
    ) -> DriveRecordMeta:
        escaped = escape_drive_query_value(name)
        query = f"'{parent_id}' in parents and name = '{escaped}' and trashed = false"
        if mime_type:
            query += f" and mimeType = '{escape_drive_query_value(mime_type)}'"

        def _list() -> dict[str, Any]:
            try:
                return self.service.files.list(
                    q=query,
                    fields=f"files({self.DEFAULT_FIELDS})",
                    pageSize=2,
                    **self._list_kwargs(),
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        response = self._call(_list)
        files = response.get("files", [])
        if not files:
            msg = f"No file named {name!r} under {parent_id}."
            raise NotFoundError(msg)
        if len(files) > 1:
            ids = [f["id"] for f in files]
            msg = f"Ambiguous file name {name!r}: {ids}"
            raise AmbiguousResourceError(msg)
        return self._meta_from_file(files[0])

    def iter_model_files(
        self,
        *,
        model_name: str,
        parent_id: str | None = None,
        scan_limit: int = 1000,
    ) -> Iterator[DriveRecordMeta]:
        token: str | None = None
        seen = 0
        while seen < scan_limit:
            page = self.scan_by_model(
                model_name=model_name,
                parent_id=parent_id,
                page_token=token,
                scan_limit=min(100, scan_limit - seen),
            )
            for item in page.items:
                yield item
                seen += 1
                if seen >= scan_limit:
                    return
            token = page.next_page_token
            if not token:
                return

    def export_native(self, file_id: str) -> tuple[bytes, str]:
        meta = self.get_metadata(file_id)
        export_mime = NATIVE_EXPORT_MIMES.get(meta.mime_type)
        if export_mime is None:
            msg = f"File {file_id} is not a supported native Workspace type."
            raise UnsupportedOperationError(msg)

        def _export() -> bytes:
            try:
                return self.service.files.export(fileId=file_id, mimeType=export_mime)
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        return self._call(_export), export_mime

    def read_text_body(self, file_id: str) -> str:
        meta = self.get_metadata(file_id)
        raw = self._download_bytes(meta)
        return raw.decode("utf-8")

    def read_file_body(
        self,
        file_id: str,
        *,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> bytes:
        meta = self.get_metadata(file_id)
        self._check_conflict(meta, expected_version, expected_fingerprint)
        if meta.mime_type in NATIVE_RICH_MIMES:
            msg = (
                f"Native Workspace file {file_id} ({meta.mime_type}) "
                "cannot be read as structured file body."
            )
            raise UnsupportedOperationError(msg)
        return self._download_bytes(meta)

    def write_file_body(
        self,
        *,
        parent_id: str,
        body: bytes,
        mime_type: str,
        model_name: str,
        record_id: str,
        file_id: str | None = None,
        name: str | None = None,
        schema_version: str | None = None,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> DriveRecordMeta:
        existing_app: dict[str, str] | None = None
        if file_id is not None:
            meta = self.get_metadata(file_id)
            self._check_conflict(meta, expected_version, expected_fingerprint)
            existing_app = meta.app_properties
            if meta.mime_type in NATIVE_RICH_MIMES:
                msg = (
                    f"Native Workspace file {file_id} ({meta.mime_type}) "
                    "cannot be updated as structured file body."
                )
                raise UnsupportedOperationError(msg)

        file_body: dict[str, Any] = {
            "mimeType": mime_type,
            "appProperties": self._merge_routing_app_properties(
                existing=existing_app,
                model_name=model_name,
                record_id=record_id,
                schema_version=schema_version,
            ),
        }
        if name is not None:
            file_body["name"] = name

        media = MediaIoBaseUpload(
            io.BytesIO(body),
            mimetype=mime_type,
            resumable=False,
        )

        def _write() -> dict[str, Any]:
            try:
                if file_id is None:
                    file_body["parents"] = [parent_id]
                    return self.service.files.create(
                        body=file_body,
                        media_body=media,
                        fields=self.DEFAULT_FIELDS,
                        supportsAllDrives=self.supports_all_drives,
                    )
                update_body = dict(file_body)
                return self.service.files.update(
                    fileId=file_id,
                    body=update_body,
                    media_body=media,
                    fields=self.DEFAULT_FIELDS,
                    supportsAllDrives=self.supports_all_drives,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        data = self._call(_write, retryable=False)
        return self._meta_from_file(data)

    def write_text_body(
        self,
        *,
        parent_id: str,
        name: str,
        body: str,
        file_id: str | None = None,
        mime_type: str = TEXT_MIME,
        model_name: str | None = None,
        record_id: str | None = None,
        schema_version: str | None = None,
        expected_version: str | None = None,
        expected_fingerprint: str | None = None,
    ) -> DriveRecordMeta:
        if model_name is not None and record_id is not None:
            return self.write_file_body(
                parent_id=parent_id,
                body=body.encode("utf-8"),
                mime_type=mime_type,
                model_name=model_name,
                record_id=record_id,
                file_id=file_id,
                name=name,
                schema_version=schema_version,
                expected_version=expected_version,
                expected_fingerprint=expected_fingerprint,
            )
        if file_id is not None:
            meta = self.get_metadata(file_id)
            if meta.mime_type in NATIVE_RICH_MIMES:
                msg = (
                    f"Native Workspace file {file_id} "
                    "cannot be replaced with text bytes."
                )
                raise UnsupportedOperationError(msg)

        media = MediaIoBaseUpload(
            io.BytesIO(body.encode("utf-8")),
            mimetype=mime_type,
            resumable=False,
        )
        file_body: dict[str, Any] = {"name": name, "mimeType": mime_type}

        def _write() -> dict[str, Any]:
            try:
                if file_id is None:
                    file_body["parents"] = [parent_id]
                    return self.service.files.create(
                        body=file_body,
                        media_body=media,
                        fields=self.DEFAULT_FIELDS,
                        supportsAllDrives=self.supports_all_drives,
                    )
                return self.service.files.update(
                    fileId=file_id,
                    body={"name": name, "mimeType": mime_type},
                    media_body=media,
                    fields=self.DEFAULT_FIELDS,
                    supportsAllDrives=self.supports_all_drives,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

        data = self._call(_write, retryable=False)
        return self._meta_from_file(data)

    def _merge_routing_app_properties(
        self,
        *,
        existing: dict[str, str] | None,
        model_name: str,
        record_id: str,
        schema_version: str | None,
    ) -> dict[str, str]:
        routing = build_app_properties(
            project_id=self.project_id,
            model_name=model_name,
            record_id=record_id,
            schema_version=schema_version,
        )
        if existing is None:
            return routing
        merged = dict(existing)
        merged.update(routing)
        return merged

    @staticmethod
    def fingerprint(meta: DriveRecordMeta) -> str:
        if meta.md5_checksum:
            return meta.md5_checksum
        if meta.version:
            return f"version:{meta.version}"
        return meta.file_id

    @staticmethod
    def _meta_from_file(data: dict[str, Any]) -> DriveRecordMeta:
        props = data.get("appProperties") or {}
        return DriveRecordMeta(
            file_id=str(data["id"]),
            name=str(data.get("name", "")),
            mime_type=str(data.get("mimeType", "")),
            version=str(data["version"]) if data.get("version") is not None else None,
            md5_checksum=data.get("md5Checksum"),
            app_properties={str(k): str(v) for k, v in props.items()},
            parents=[str(p) for p in data.get("parents", [])],
        )

    def _check_conflict(
        self,
        meta: DriveRecordMeta,
        expected_version: str | None,
        expected_fingerprint: str | None,
    ) -> None:
        if expected_fingerprint is not None:
            current = self.fingerprint(meta)
            if current != expected_fingerprint:
                msg = (
                    f"Conflict for file {meta.file_id}: expected fingerprint "
                    f"{expected_fingerprint}, found {current}."
                )
                raise ConflictError(msg)
            return
        if expected_version is not None and meta.version != expected_version:
            msg = (
                f"Conflict for file {meta.file_id}: expected version "
                f"{expected_version}, found {meta.version}."
            )
            raise ConflictError(msg)

    def _download_bytes(self, meta: DriveRecordMeta) -> bytes:
        if meta.mime_type in NATIVE_RICH_MIMES:
            data, _ = self.export_native(meta.file_id)
            return data

        def _download() -> bytes:
            get_media = getattr(self.service.files, "get_media", None)
            if get_media is None:
                msg = f"Drive service cannot download media for file {meta.file_id}."
                raise UnsupportedOperationError(msg)
            try:
                request = get_media(
                    fileId=meta.file_id,
                    supportsAllDrives=self.supports_all_drives,
                )
            except HttpError as exc:
                raise self._normalize_http_error(exc) from exc

            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
            return buffer.getvalue()

        return self._call(_download)
