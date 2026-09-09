"""Public package surface for google-drive-model."""

from importlib.metadata import PackageNotFoundError, version

from gdm.auth import login_interactive
from gdm.client import GDM, GDMClient
from gdm.collection.collection import DriveCollection
from gdm.collection.content import ContentHandle
from gdm.errors import (
    AmbiguousResourceError,
    ConflictError,
    ForeignProjectError,
    GDMError,
    InvalidDrivePropertyError,
    InvalidForeignKeyError,
    InvalidModelConfigError,
    MissingOptionalDependencyError,
    NotFoundError,
    PartialCommitError,
    PermissionDeniedError,
    QueryEvaluationError,
    ReadOnlySessionError,
    ResultError,
    ScanLimitExceeded,
    UnknownSourceFieldError,
    UnsupportedOperationError,
)
from gdm.fields import Field, GDMFieldMetadata
from gdm.indexes import GdmIndexError, IndexStore, StaleIndexError, StalePolicy
from gdm.model import DriveObjectMixin, GDMModel
from gdm.project import ResourceKind
from gdm.query import Select, select
from gdm.session import Result, Session

__all__ = [
    "GDM",
    "AmbiguousResourceError",
    "ConflictError",
    "ContentHandle",
    "DriveCollection",
    "DriveObjectMixin",
    "Field",
    "ForeignProjectError",
    "GDMClient",
    "GDMError",
    "GDMFieldMetadata",
    "GDMModel",
    "GdmIndexError",
    "IndexStore",
    "InvalidDrivePropertyError",
    "InvalidForeignKeyError",
    "InvalidModelConfigError",
    "MissingOptionalDependencyError",
    "NotFoundError",
    "PartialCommitError",
    "PermissionDeniedError",
    "QueryEvaluationError",
    "ReadOnlySessionError",
    "ResourceKind",
    "Result",
    "ResultError",
    "ScanLimitExceeded",
    "Select",
    "Session",
    "StaleIndexError",
    "StalePolicy",
    "UnknownSourceFieldError",
    "UnsupportedOperationError",
    "__version__",
    "login_interactive",
    "select",
]

try:
    __version__ = version("google-drive-model")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0+unknown"
