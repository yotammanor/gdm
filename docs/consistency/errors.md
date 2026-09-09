# Consistency and errors

GDM exposes explicit failure modes instead of pretending Drive is transactional.

## Exception overview

| Exception | When |
| --- | --- |
| `ValidationError` (Pydantic) | Boundary validation failed |
| `AmbiguousResourceError` | Duplicate names where one match is required |
| `ConflictError` | Source changed since instance was loaded |
| `PartialCommitError` | Later commit batch failed after earlier success |
| `ReadOnlySessionError` | Mutating call on a read-only session |
| `ForeignProjectError` | Resource tagged for another project without adoption mode |
| `UnknownSourceFieldError` | Strict mapping rejects unknown source fields |
| `ScanLimitExceeded` | Scan would exceed configured `scan_limit` |
| `NotFoundError` | Missing record or out-of-collection file ID |
| `UnsupportedOperationError` | Capability not available for MIME type |
| `QueryEvaluationError` / `ResultError` | Invalid query or wrong result cardinality |

All shipped exceptions live in `gdm.errors` and are re-exported from `gdm`.

## ConflictError {#conflicterror}

Before updates GDM re-reads the source and compares version or row fingerprint:

```python
from gdm.errors import ConflictError

try:
    session.commit()
except ConflictError as exc:
    print(exc.locator, exc.expected_version, exc.actual_version)
```

Collection models track **Drive metadata** and **metadata-table versions** separately. A metadata conflict does not cause GDM to upload or rewrite native content.

!!! platform-limit "Race window"
    Two writers can still race between re-read and write. Stronger coordination requires
    single-writer designs or an external database.

## PartialCommitError {#partialcommiterror}

`Session.commit()` applies pending operations sequentially (deletes, creates,
updates). If a later operation fails after earlier API calls succeeded:

```python
from gdm.errors import PartialCommitError

try:
    session.commit()
except PartialCommitError as exc:
    print("completed:", exc.completed)
    print("pending:", exc.pending)
```

Earlier successful API calls are **not rolled back**. Application code must reconcile or
retry idempotently.

## Delete semantics

| Storage | `session.delete()` | Explicit permanent delete |
| --- | --- | --- |
| `collection` | Trashes Drive file | `collection.delete_permanently(file_id)` |
| `sheet` | Deletes worksheet row | N/A (row removal) |
| Legacy `file` codec | Deletes Drive file | Same as collection trash path |

`detach` removes folder membership without deleting the underlying file.

## rollback()

`session.rollback()` clears unflushed in-memory changes only.

## Retries

Transient Google statuses (`429`, selected `403` rate limits, `5xx`) retry with
exponential backoff and jitter for safe reads. Non-idempotent creates, appends,
permission changes, and arbitrary batch mutations execute once; after an ambiguous
network failure, reconcile remote state before retrying.

## Related

- [Drive collections tutorial](../tutorial/collection.md)
- [Migrations](../migrations/index.md)
- [Design: concurrency](../design.md#decision-8-concurrency-and-failure-semantics)
