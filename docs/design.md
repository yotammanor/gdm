# GDM design

Status: accepted for the first public release.

GDM, short for **Google Drive Model**, brings Pydantic models and a small,
SQLModel-inspired session/query API to Google Drive. It is designed for drives
that already contain useful, human-edited data—not only for storage created by
GDM.

This document records the product and architecture decisions behind the public
API. The limitations are part of the contract: Google Drive is collaborative
storage, not an ACID database.

## Product principles

1. **Adopt progressively.** A project can bind one worksheet or folder and
   ignore everything else. A model can map a subset of fields while preserving
   unknown columns and JSON keys.
2. **Keep Drive useful without GDM.** Sheet rows remain readable, files remain
   ordinary files, links are Drive IDs or URLs, and metadata is stored in a
   small, documented project folder.
3. **Validate at the boundary.** Pydantic validates reads and writes. Invalid
   human edits produce precise diagnostics rather than silent coercion or data
   loss.
4. **Be honest about consistency.** `Session.commit()` batches a unit of work,
   but it is not a cross-file transaction. Conflicts and partial failures are
   explicit.
5. **Delegate access control.** Google Drive permissions are the security
   boundary. GDM provides inspection and convenience helpers; it does not
   invent row-level security.
6. **Isolate by root.** Every GDM project is anchored to one Drive folder.
   Multiple applications and environments can use the same account safely by
   choosing different root folder IDs.

## Decision 1: Pydantic-first, not a SQLModel subclass

GDM takes inspiration from SQLModel's one-class validation and persistence
experience, `Field()`, `Session`, `select()`, and result objects. It does not
subclass SQLModel and does not depend on SQLAlchemy.

SQLModel's implementation joins Pydantic's model system to SQLAlchemy's
declarative metaclass, columns, relationships, query compiler, and transaction
engine. Drive and Sheets do not provide SQL tables, joins, constraints,
transactions, or row locks. Extending SQLModel would either expose APIs that
cannot honor their SQL semantics or require a fake SQLAlchemy dialect whose
translation is mostly client-side. Both choices create surprising behavior and
a large dependency surface.

GDM instead extends Pydantic directly and adds:

- `GDMModel` for persisted and non-persisted models;
- `Field()` metadata for keys, source names, indexes, and links;
- a typed expression tree produced by class-level field references;
- `Session` and `select()` with documented local and remote execution rules;
- backend protocols for Drive files, Google Sheets, and test doubles.

This keeps model validation familiar while allowing Drive-native behavior to be
represented honestly.

## Decision 2: official Google clients

The default transport is:

- `google-api-python-client` for Drive v3 and Sheets v4;
- `google-auth` for credentials and refresh;
- `google-auth-oauthlib` as the optional `oauth` extra for installed-app login.

There is no generated Google Cloud Python client for the Workspace Drive API.
PyDrive2 wraps the same discovery client and hides controls GDM needs, including
partial response fields, shared-drive flags, batching, changes, and resumable
media. A bespoke HTTP client would have to recreate discovery schemas, refresh,
multipart batches, retries, resumable uploads, and error normalization.

GDM therefore pays the official client's installation-size cost in exchange for
API completeness and support. All Google calls live behind narrow protocols so
a future transport can be added without changing models.

GDM uses the Sheets v4 service directly instead of gspread. This avoids a second
abstraction and lets one credential, retry policy, error layer, and batch model
serve both APIs.

## Decision 3: two first-class storage shapes

### Drive documents

`storage="file"` stores one model instance in one Drive file. JSON is the
canonical structured codec. Text and bytes codecs support existing documents
where one field represents the body. Metadata fields can map to Drive metadata
such as `name`, `mimeType`, and `parents`.

GDM stores only small routing keys in private `appProperties`:

- `gdm_project`: stable project ID;
- `gdm_model`: model's stable storage name;
- `gdm_id`: logical primary key;
- `gdm_schema`: schema version.

Drive limits private properties to 30 per app per file and limits each
key/value string to 124 UTF-8 bytes. Full records and indexes never live there.

Native Google Docs, Sheets, and Slides are not writable as ordinary bytes.
Sheets have their own backend. Native Docs can be discovered and exported; rich
structural mutation belongs in a future Docs adapter rather than pretending an
export can be uploaded in place.

### Sheet tables

`storage="sheet"` maps one model to a worksheet. Headers are field source names
and rows are model instances. GDM reserves these columns when it manages row
identity:

- `_gdm_id`: stable UUID when the model has no explicit primary key;
- `_gdm_version`: optimistic version maintained by GDM;
- `_gdm_updated_at`: UTC timestamp of the latest GDM write.

Existing sheets can instead nominate an existing unique column as the primary
key. Unknown columns are preserved because updates target only mapped cells.
Reads can ignore them, expose them on the instance, or reject them according to
the mapping mode.

Sheets are the first relational representation. Foreign keys are ordinary
typed cells containing another model's key or Drive file ID. Relationships are
resolved client-side and never imply storage-enforced referential integrity.

## Decision 4: progressive mapping modes

Each persisted model chooses one mode:

- `partial` (default): require declared fields, ignore and preserve unknown
  source fields;
- `permissive`: preserve unknown fields and expose them through
  `model.gdm_extra`;
- `strict`: reject unknown source fields and schema drift.

The explorer and inference tools never mutate a source. Adoption is an explicit
binding operation. Adding GDM system columns or metadata requires an explicit
initialization or migration.

Bindings may use immutable resource IDs or discover by path/name. A discovered
name is persisted as an ID before writes because Drive permits duplicate names.
Ambiguous discovery is always an error.

## Decision 5: root-scoped projects and multi-tenancy

A `Project` has a stable UUID and one Drive root folder ID. Its internal
metadata lives under:

```text
<root>/
└── .gdm/
    ├── manifest.json
    ├── registry.json
    ├── schemas/
    ├── migrations/
    └── indexes/
```

The manifest includes the project UUID and format version. The registry maps
logical model names to immutable file, folder, spreadsheet, and worksheet IDs.
Models never scan outside the configured root unless the caller explicitly
asks the explorer to do so.

Different projects, tenants, or environments should use different roots. They
may share credentials and even a parent folder. A session refuses resources
tagged for another project unless opened in explicit adoption mode.

The `.gdm` folder is ordinary Drive data so projects work across machines.
Local token and cache paths are user-specific and never uploaded.

## Decision 6: drift-aware, forward migrations

There is no Alembic dependency because Drive has no database DDL or transaction
log. GDM migrations are ordered, idempotent operations with checkpoints in the
project metadata folder.

Supported operation families include:

- add a field/header with a validated default;
- rename a field/header while retaining aliases;
- transform values with validation and a dry-run report;
- add or backfill stable row IDs and versions;
- move or retag file collections;
- rebuild an index.

Dropping fields, deleting source values, replacing whole worksheets, and
changing MIME types are destructive and require an explicit destructive flag.
The default plan is additive. Every plan can be inspected without mutation and
reports invalid rows/files before apply.

Schemas are snapshots of model JSON Schema plus GDM field metadata. Drift is the
difference among the model snapshot, the last applied snapshot, and the live
source. This three-way view distinguishes a code migration from a human edit.

Migration application is resumable. Each operation records its completion and
can be safely retried. Rollback exists only when a migration declares a reverse
operation; GDM never promises transactional rollback across resources.

## Decision 7: query execution and indexes

`select(Model).where(...)` builds a backend-independent expression tree.
Execution is deliberately bounded:

- Drive metadata predicates supported by Drive `q` and indexed
  `appProperties` are pushed down;
- sheet reads use bounded ranges and evaluate model predicates locally;
- unsupported file-content predicates require loading candidate files;
- joins and relationships are client-side with batched prefetch.

Callers can set `scan_limit`; GDM refuses an accidental unbounded content scan
when the configured limit is exceeded.

`Field(index=True)` enables an optional sidecar index in
`.gdm/indexes/<model>/<field>.json`. An index maps a canonical, type-tagged
value to logical record IDs and includes a source fingerprint. It is a speed
hint, never the source of truth. Direct edits can make it stale, so reads verify
the fingerprint and either rebuild, fall back to a scan, or raise according to
the session policy.

Indexes are updated with GDM writes and can be rebuilt with `gdm index rebuild`.
Unique indexes are checked optimistically; they are not a security or
cross-client uniqueness guarantee.

## Decision 8: concurrency and failure semantics

Drive v3 exposes a monotonically increasing file `version`, checksums for
binary files, and revision information, but it does not document a general
conditional update precondition for file writes. Sheets batches are atomic
within one `spreadsheets.batchUpdate` request, not across separate API calls or
collaborator edits.

GDM records the source version or row fingerprint when materializing a model.
Before an update it re-reads the source and raises `ConflictError` when the
known source changed. This prevents many lost updates but cannot remove the
read/write race. Applications needing stronger coordination must use a
single-writer architecture or another database.

`Session.commit()`:

1. validates all pending models locally;
2. checks conflicts and unique indexes;
3. groups and batches writes by resource;
4. applies writes in deterministic dependency order;
5. updates schemas/indexes after source writes;
6. raises `PartialCommitError` with completed and pending operations if a later
   group fails.

`rollback()` only clears unflushed local changes. It cannot undo remote writes.

Retry policy covers documented transient statuses (`429`, selected `403` rate
limits, and `5xx`) with exponential backoff and jitter. Mutating retries use
stable request IDs or idempotency checks where the API permits.

## Decision 9: permissions

Drive and shared-drive ACLs remain authoritative. GDM offers:

- capability inspection (`canEdit`, `canShare`, download restrictions);
- permission listing and carefully named `share()`/`unshare()` helpers;
- read-only sessions and model-level write guards;
- service-account diagnostics explaining that roots must be shared;
- `supportsAllDrives=True` and the corresponding list flags on relevant calls.

There is no row-level security in a worksheet. If rows require different
audiences they must live in separately permissioned files or folders. Protected
ranges are an editing aid, not an authorization boundary.

Installed-user OAuth is the recommended interactive experience. Service
accounts are recommended for automation after a root is explicitly shared.
Application Default Credentials are supported. Tokens are stored outside the
project using platform-appropriate user data directories and are never logged.

The narrow `drive.file` scope is suitable when GDM creates or the user opens the
resources. Exploring and adopting arbitrary existing Drive content requires a
broader Drive scope and may require Google's restricted-scope verification for
distributed applications. GDM requests only scopes required by the command.

## Decision 10: sync and cache

The Drive Changes API is the invalidation source for account- or shared-drive
sync. A stored start page token enables incremental polling. Push notification
channels are optional accelerators: notifications contain no resource body,
expire, and still require polling changes.

The first release provides polling primitives and stateless reads. A local
cache can be layered on later without changing model semantics. Cache entries
must be keyed by credential principal, project ID, resource ID, and version.

## Decision 11: sync API first

The official discovery client is synchronous. GDM's first-class API is
synchronous for scripts, agents, notebooks, and command-line applications.
An `AsyncSession` implemented only by dispatching blocking requests to threads
would provide questionable cancellation and rate-limit behavior, so it is not
presented as native async. Applications can isolate sync sessions in their own
worker/thread today. A future async transport can implement the backend
protocol without changing models.

## Practical scale envelope

GDM optimizes for human-scale and team-scale data:

- thousands of Drive files per mapped collection;
- tens of thousands of sheet rows with batching and indexes;
- bounded client-side filters and relationships.

The hard platform limits are higher, but latency, quota cost, direct edits, and
client-side execution become the dominant constraints first. `gdm doctor`
warns about large collections, duplicate paths, stale indexes, invalid rows,
and expensive query plans. BigQuery, Cloud SQL, or a document database is the
recommended next backend when workloads require server-side joins, high write
rates, strict transactions, or hundreds of thousands of frequently queried
records.

## Packaging name

The import package and command are `gdm`, and the repository is `gdm`. The PyPI
name `gdm` is already owned by an unrelated Git dependency manager, so the
distribution is named `google-drive-model`:

```console
pip install google-drive-model[oauth]
```

```python
from gdm import GDMModel, Session, select
```

This distinction is called out in package metadata and documentation.

## References

- [Google Drive API Python quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
- [Drive files resource](https://developers.google.com/workspace/drive/api/reference/rest/v3/files)
- [Drive custom properties](https://developers.google.com/workspace/drive/api/guides/properties)
- [Drive shared-drive support](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives)
- [Drive changes](https://developers.google.com/workspace/drive/api/guides/manage-changes)
- [Drive limits](https://developers.google.com/workspace/drive/api/guides/limits)
- [Sheets values](https://developers.google.com/workspace/sheets/api/guides/values)
- [Sheets batch updates](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)
- [Sheets limits](https://developers.google.com/workspace/sheets/api/limits)
- [SQLModel](https://sqlmodel.tiangolo.com/)
