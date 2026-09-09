"""GDM-specific exceptions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class GDMError(Exception):
    """Base class for GDM runtime errors."""


class NotFoundError(GDMError):
    """Raised when a Drive or Sheets resource cannot be found."""


class AmbiguousResourceError(GDMError):
    """Raised when discovery matches multiple resources."""


class ForeignProjectError(GDMError):
    """Raised when a resource belongs to a different GDM project."""


class PermissionDeniedError(GDMError):
    """Raised when credentials lack access to a resource."""


class UnsupportedOperationError(GDMError):
    """Raised when the backend cannot perform the requested operation."""


class InvalidDrivePropertyError(UnsupportedOperationError):
    """Raised when Drive custom property limits would be exceeded."""


class ConflictError(GDMError):
    """Raised when an optimistic concurrency check detects a source change."""

    def __init__(
        self,
        message: str,
        *,
        locator: Any | None = None,
        expected_version: str | None = None,
        actual_version: str | None = None,
    ) -> None:
        super().__init__(message)
        self.locator = locator
        self.expected_version = expected_version
        self.actual_version = actual_version


class ReadOnlySessionError(GDMError):
    """Raised when a mutating operation is attempted on a read-only session."""


class ScanLimitExceeded(GDMError):
    """Raised when a bounded scan would exceed the configured limit."""

    def __init__(
        self,
        message: str,
        *,
        limit: int,
        observed: int,
        model_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.limit = limit
        self.observed = observed
        self.model_name = model_name


class UnknownSourceFieldError(GDMError):
    """Raised when strict mapping encounters undeclared source fields."""

    def __init__(self, message: str, *, fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fields = fields


class QueryEvaluationError(GDMError):
    """Raised when a query expression cannot be evaluated."""


class ResultError(GDMError):
    """Raised when a result helper expects a different number of rows."""


class InvalidModelConfigError(GDMError):
    """Raised when a model class keyword configuration is invalid."""


class MissingOptionalDependencyError(GDMError):
    """Raised when an optional extra package is required but not installed."""

    def __init__(self, message: str, *, extra: str) -> None:
        super().__init__(message)
        self.extra = extra


class InvalidForeignKeyError(GDMError):
    """Raised when ``foreign_key`` metadata references an unknown model field."""


class MappingMode(StrEnum):
    """Progressive mapping modes for persisted models."""

    PARTIAL = "partial"
    PERMISSIVE = "permissive"
    STRICT = "strict"


@dataclass
class CommitOperation:
    """One pending or completed commit operation."""

    action: str
    model_name: str
    locator: Any
    detail: str = ""


@dataclass
class PartialCommitError(GDMError):
    """Raised when commit applies some operations but later ones fail."""

    message: str
    completed: list[CommitOperation] = field(default_factory=list)
    pending: list[CommitOperation] = field(default_factory=list)
    cause: Exception | None = None

    def __str__(self) -> str:
        return self.message
