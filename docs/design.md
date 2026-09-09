# GDM design

Status: accepted for the first public release.

!!! tip "Navigation"
    User-facing guides: [Drive collections tutorial](tutorial/collection.md),
    [Sheet tables tutorial](tutorial/sheets.md), [API reference](api/index.md).
    Implementation acceptance checks: [implementation plan](implementation-plan.md).

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

## Decision 3: collections and native Drive objects are the center

A Drive folder is a first-class `Collection[T]`. Its direct children are the
objects in the collection and each child's immutable Drive file ID is its
identity. Names are presentation metadata, not keys: Drive permits duplicate
names and GDM preserves that behavior.

```python
class CampaignAsset(
    GDMModel,
    storage="collection",
    collection="Campaigns/Launch",
    mapping="partial",
):
    drive_id: str = Field(drive="id")
    name: str = Field(drive="name")
    mime_type: str = Field(drive="mimeType")
    campaign: str | None = Field(metadata="campaign")
    approved: bool = Field(default=False, metadata="approved")


assets = client.collection(CampaignAsset)
images = assets.where_mime("image/*")
```

`storage="file"` remains a compatibility alias for
`storage="collection"`. It does **not** mean that GDM owns or replaces file
contents.

### Folder-native collection semantics

Collections deliberately expose Drive behavior:

- direct children are included by default; recursion is explicit;
- nested folders can be opened as subcollections;
- duplicate names return multiple matches and singular lookup raises an
  ambiguity error;
- shortcuts remain shortcuts and expose their target ID/resource key;
- adopting an object already in the folder adds metadata without moving it;
- adding an object from another folder requires an explicit `move=True` or
  creates a shortcut with `shortcut=True`;
- removing an object from a collection does not silently delete it: `detach`,
  `move`, `trash`, and permanent deletion are distinct operations;
- collection queries can filter Drive metadata remotely and GDM metadata via a
  bounded metadata-table/index lookup;
- a model can select MIME families while unmodeled sibling files remain visible
  through collection inventory and untouched by typed queries.

The collection's binding is persisted as the folder ID, not a path. Paths and
names are discovery conveniences that must resolve uniquely before writes.

### Object model: metadata and content are separate

A collection object has two orthogonal parts:

1. **ORM-managed metadata**, validated by the Pydantic model.
2. **Native content**, owned by the Drive file and accessed explicitly.

Built-in metadata fields use `Field(drive=...)` and map to supported Drive file
fields such as `name`, `description`, `starred`, and parents. Read-only fields
such as `id`, `mimeType`, `createdTime`, `modifiedTime`, checksums, links,
owners, and capabilities are materialized but rejected in update masks.

Application fields use `Field(metadata=...)`. Their canonical representation is
a collection metadata table keyed by Drive file ID. The default table is a
Google Sheet under the project's `.gdm/collections/` area, making custom
metadata typed, relational, inspectable, and still editable outside GDM. A
JSON sidecar is available for projects that do not enable the Sheets API.
Small routing/index hints may be mirrored into private `appProperties`, but
those properties are never the full object record.

This split means a model can add application concepts to a folder of existing
Docs, Slides, images, PDFs, videos, or arbitrary files without converting them.
Metadata writes use Drive `files.update` and targeted metadata-table cells;
they never upload media unless the caller explicitly invokes a content method.

### Native content handles

Every materialized collection object exposes a lazy `content` handle. Merely
querying metadata performs no content download.

```python
brief = assets.get(file_id)

# Opaque/binary content
data = brief.content.download()
brief.content.upload(new_bytes, mime_type="image/png")

# Native Workspace content stays native
document = brief.content.google_doc()  # Docs API document resource
document.batch_update([...])  # guarded by Docs requiredRevisionId

slides = brief.content.google_slides()  # Slides API presentation resource
slides.batch_update([...])  # guarded by Slides requiredRevisionId

# Explicit portable rendering; never confused with the native source
pdf = brief.content.export("application/pdf")
```

Content capabilities are selected by MIME type:

- blobs, images, PDFs, and other uploaded files support streamed
  download/resumable upload;
- Google Docs use the Docs API for structured reads and batch updates;
- Google Slides use the Slides API;
- Google Sheets can be opened as native sheet content or bound as a relational
  table;
- export creates a portable view and never becomes an in-place native update;
- unsupported native mutations raise a capability error with the relevant
  official API escape hatch.

GDM exposes the underlying authenticated API services from a content handle for
advanced operations. This is intentional SDK interoperability, not a leaky
fallback to hide an incomplete storage model.

### Structured document codecs

JSON, text, YAML, and bytes are content adapters for **opt-in structured file models**
(`storage="file"` with an explicit `codec=`). Each codec serializes declared model
fields into the Drive file body on write and parses them on read:

- `codec="json"`: multi-field JSON object; partial/permissive mapping preserves
  unknown keys; optimistic concurrency via Drive version/fingerprint.
- `codec="text"`: exactly one `str` body field holds the complete UTF-8 file
  contents (not line records).
- `codec="bytes"`: exactly one `bytes` body field; opaque binary with configured
  or default MIME type.
- `codec="yaml"`: YAML mapping with JSON-equivalent mapping semantics; optional
  PyYAML extra (`google-drive-model[yaml]`).

Routing uses private `appProperties` only (`gdm_project`, `gdm_model`, `gdm_id`,
optional `gdm_schema`). Native Google Docs, Slides, and other Workspace MIME types
reject codec body writes.

GDM stores only small routing keys in private `appProperties`:

- `gdm_project`: stable project ID;
- `gdm_model`: model's stable storage name;
- `gdm_schema`: schema version.

Drive limits private properties to 30 per app per file and limits each
key/value string to 124 UTF-8 bytes. Full records and indexes never live there.

### Sheet tables

`storage="sheet"` maps one model to a worksheet. This is a separate,
row-oriented collection for data whose native content is a table. Headers are
field source names and rows are model instances. GDM reserves these columns
when it manages row identity:

- `_gdm_id`: stable UUID when the model has no explicit primary key;
- `_gdm_version`: optimistic version maintained by GDM;
- `_gdm_updated_at`: UTC timestamp of the latest GDM write.

Existing sheets can instead nominate an existing unique column as the primary
key. Unknown columns are preserved because updates target only mapped cells.

When the bound worksheet is **empty**, the first `session.add()` / create
initializes the header row from declared field `source` names plus system
columns (`_gdm_id`, `_gdm_version`, `_gdm_updated_at`) when GDM manages row identity.
Reads can ignore them, expose them on the instance, or reject them according to
the mapping mode.

Sheets provide both standalone relational tables and the metadata plane for
native Drive collections. Foreign keys are ordinary typed cells containing
another row key or Drive file ID. Relationships are resolved client-side and
never imply storage-enforced referential integrity.

## Decision 4: progressive mapping modes

Each persisted model chooses one mode:

- `partial` (default): require declared fields, ignore and preserve unknown
  source fields;
- `permissive`: preserve unknown fields and expose them through
  `model.gdm_extra`;
- `strict`: reject unknown source fields and schema drift.

The explorer and inference tools never mutate a source. Adoption is an explicit
binding operation. Adding GDM system columns to a **non-empty** worksheet or
changing collection metadata requires an explicit migration. An **empty**
worksheet gets model and system headers on the first `Session` create.

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
    ├── collections/
    │   └── <model-name>/
    │       └── metadata spreadsheet (Google Sheet) or JSON sidecar adapter
```

Collection metadata bindings (spreadsheet ID, worksheet name) are recorded in
`registry.json` under each model's `extra` fields—not in fixed JSON filenames.

The manifest includes the project UUID and format version. The registry maps
logical model names to immutable file, folder, spreadsheet, and worksheet IDs.
Models never scan outside the configured root unless the caller explicitly
asks the explorer to do so or opens the client with `adoption_mode=True`.

Different projects, tenants, or environments should use different roots. They
may share credentials and even a parent folder. A session refuses resources
outside its root or tagged for another project unless opened in explicit
adoption mode.

The Drive `.gdm` folder is ordinary Drive data, so authoritative manifests,
bindings, and collection metadata work across machines. Source-controlled tooling
state is deliberately local in v0.1:

```text
<local-project>/.gdm/
├── schemas/       # current/applied model snapshots
├── migrations/    # resumable checkpoints
└── indexes/       # rebuildable performance caches
```

OAuth tokens and local caches are user-specific and never uploaded. These local
directories are tooling state, not Drive resources or sources of truth.

## Decision 6: drift-aware, forward migrations

There is no Alembic dependency because Drive has no database DDL or transaction
log. GDM migrations are ordered operations with deterministic operation IDs and
checkpoints in the local project metadata folder.

Supported operation families include:

- add a field/header with a validated default;
- rename a field/header (applications may retain a Pydantic validation alias during rollout);
- transform supported scalar values with row-specific validation results;
- add or backfill stable row IDs and versions;
- bind a folder as a collection without rewriting any child content;
- add, rename, or backfill collection metadata columns;
- move or retag file collections;
- rebuild an index.

Dropping fields, deleting source values, and replacing whole worksheets are
destructive and require an explicit destructive flag. Automatic MIME conversion
is blocked in v0.1 even with that flag; callers must use an explicit
export/create/verify workflow.
Changing a native document's content or moving collection members is never an
implicit consequence of a metadata migration. The default plan is additive.
Every plan can be inspected without mutation. Dry-run planning reports the
operations and destructive boundaries without reading native bodies; transform
errors are reported with row identifiers during apply.

Schemas are snapshots of model JSON Schema plus GDM field metadata. Drift is the
difference among the model snapshot, the last applied snapshot, and the live
source. This three-way view distinguishes a code migration from a human edit.

Migration application is resumable. Each operation records its completion and
can be safely retried. Rollback exists only when a migration declares a reverse
operation; GDM never promises transactional rollback across resources.

## Decision 7: query execution and indexes

`select(Model).where(...)` builds a backend-independent expression tree.
Execution is deliberately bounded:

- backends report `supports_metadata_pushdown=False`; v0.1.0 does **not** claim
  Drive `q` or `appProperties` predicate pushdown;
- sheet and collection reads use **bounded scans** (or targeted reads when an
  index resolves record IDs) and evaluate model predicates locally;
- when `Field(index=True)` sidecar indexes exist and the `where` clause matches,
  lookups use verified indexes keyed by Drive file ID or row primary key, with
  fingerprint checks and scan fallback;
- native content is never downloaded as an implicit query side effect;
- joins and relationships are client-side with batched prefetch.

Callers can set `scan_limit`; GDM refuses an accidental unbounded metadata scan
when the configured limit is exceeded. Content search is an explicit adapter
or Google Drive full-text operation, not an ORM field predicate.

`Field(index=True)` enables an optional local sidecar index in
`<local-project>/.gdm/indexes/<model>/<field>.json`. An index maps a canonical, type-tagged
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

Collection updates track Drive metadata and custom metadata-table versions
separately. A metadata conflict does not cause GDM to upload or rewrite native
content. Content handles preflight the materialized Drive version before binary
or Sheets mutations. Docs and Slides updates additionally send the native
`requiredRevisionId` server precondition, which closes the read/write race for
those APIs. Drive binary uploads and Sheets expose no equivalent general
precondition, so their check remains best effort.

`Session.commit()`:

1. validates all pending models locally;
2. checks conflicts and unique indexes;
3. applies pending operations **sequentially** in deterministic order (deletes,
   then creates, then updates);
4. maintains sidecar indexes after each successful write;
5. raises `PartialCommitError` with completed and pending operations if a later
   operation fails after earlier API calls succeeded.

Applications save current model snapshots under `.gdm/schemas/` with
`SchemaRegistry`; migration apply/rollback maintains the last-applied snapshot.
Session commits never rewrite schema files implicitly.

`rollback()` only clears unflushed local changes. It cannot undo remote writes.

Retry policy covers documented transient statuses (`429`, selected `403` rate
limits, and `5xx`) with exponential backoff and jitter for safe reads. GDM does
not automatically repeat non-idempotent creates, appends, permission changes, or
arbitrary batch mutations: an ambiguous failure is surfaced so the caller can
reconcile remote state before retrying.

## Decision 9: permissions

Drive and shared-drive ACLs remain authoritative. GDM offers:

- capability inspection (`canEdit`, `canShare`, download restrictions);
- permission listing and carefully named `share()`/`unshare()` helpers;
- read-only sessions and an explicit `PermissionsManager.ensure_writable()` preflight helper;
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
warns about large collections, duplicate direct-child names, stale local indexes,
metadata reconciliation, schema drift, and scan-only query paths. BigQuery,
Cloud SQL, or a document database is the recommended next backend when workloads
require server-side joins, high write rates, strict transactions, or hundreds of
thousands of frequently queried records.

## Packaging name

The import package and command are `gdm`, and the repository is `gdm`. The PyPI
name `gdm` is already owned by an unrelated Git dependency manager, so the
distribution is named `google-drive-model`.

Install the release from PyPI:

```console
pip install "google-drive-model[oauth]"
```

For an editable source checkout:

```console
git clone https://github.com/yotammanor/gdm.git
cd gdm
uv sync --all-groups --frozen
```

```python
from gdm import GDMModel, Session, select
```

The distribution/import-name distinction is called out in package metadata and
documentation.

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
