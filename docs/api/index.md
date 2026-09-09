# API reference

Public Python surface for **`gdm`** (import name). Distribution package:
**`google-drive-model`**.

## Quick reference {#quick-reference}

| Symbol | Role |
| --- | --- |
| `GDM` / `GDMClient` | Connect, bind, explore, permissions |
| `DriveCollection` | Typed folder collection |
| `ContentHandle` | Lazy native content |
| `GDMModel` | Pydantic base with storage metadata |
| `DriveObjectMixin` | Standard Drive identity fields |
| `Field` | Pydantic field + GDM metadata |
| `Session` | Unit-of-work queries and commits |
| `select()` | Query builder |
| `ResourceKind` | Registry binding kind enum |
| `IndexStore` / `StalePolicy` | Optional local index configuration |

## Package exports {#gdm}

::: gdm
    options:
      show_submodules: false
      members:
        - GDM
        - GDMClient
        - DriveCollection
        - ContentHandle
        - DriveObjectMixin
        - GDMModel
        - Field
        - GDMFieldMetadata
        - Session
        - Result
        - ResourceKind
        - IndexStore
        - StalePolicy
        - select
        - Select
        - login_interactive
      show_if_no_docstring: true

## Errors {#errors}

::: gdm.errors
    options:
      filters:
        - "!^__"
      members:
        - GDMError
        - AmbiguousResourceError
        - ConflictError
        - ForeignProjectError
        - InvalidDrivePropertyError
        - InvalidForeignKeyError
        - InvalidModelConfigError
        - MissingOptionalDependencyError
        - NotFoundError
        - PartialCommitError
        - PermissionDeniedError
        - QueryEvaluationError
        - ReadOnlySessionError
        - ResultError
        - ScanLimitExceeded
        - UnknownSourceFieldError
        - UnsupportedOperationError

## Project and binding {#resourcekind}

::: gdm.project
    options:
      members:
        - Project
        - RegistryEntry
        - ResourceKind

## Migrations {#migrations}

::: gdm.migrations
    options:
      members:
        - MigrationPlanner
        - MigrationEngine
        - MigrationPlan
        - MigrationOperation

Use `client.migration_target(...)` for a production Drive/Sheets target and
`client.configure_indexes(...)`, `client.rebuild_index(...)`, or
`client.rebuild_model_indexes(...)` for local sidecar indexes.

## Indexes {#indexes}

::: gdm.indexes
    options:
      members:
        - IndexStore
        - StalePolicy
        - GdmIndexError
        - StaleIndexError

## Change polling {#change-polling}

::: gdm.sync
    options:
      members:
        - ChangesPoller
        - SyncCheckpoint

## Testing helpers {#testing}

::: gdm.testing
    options:
      members:
        - MemoryBackend
        - memory_backend
        - memory_session
        - session_context

## Submodule index

| Module | Purpose |
| --- | --- |
| `gdm.client` | `GDM` / `GDMClient` entry point |
| `gdm.collection` | `DriveCollection`, `ContentHandle`, metadata stores |
| `gdm.migrations` | Plan/apply additive migrations |
| `gdm.explore` | Read-only Drive inventory |
| `gdm.infer` | Model inference from metadata or local samples |
| `gdm.permissions` | `PermissionsManager` ACL helpers |
| `gdm.indexes` | Sidecar index maintenance |
| `gdm.schema` | Schema snapshots and drift detection |

## Design document

Architecture decisions and platform limits:
[Design decisions](../design.md).
