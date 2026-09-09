# Implementation plan

This plan turns the decisions in [the design](design.md) into verifiable
deliverables. User-facing walkthroughs: [tutorial](tutorial/index.md),
[collections](tutorial/collection.md), [API reference](api/index.md).

## Release status (v0.1.2)

| Milestone | Status |
| --- | --- |
| Core library (models, session, memory/registry backends) | Implemented — covered by unit tests |
| Sidecar indexes, unique checks, maintenance on write | Implemented |
| Migrations + declared rollback operations | Implemented — see `gdm.migrations` |
| CLI (init, explore, infer, adopt, doctor, migrate, index) | Implemented — dry-run default |
| GitHub Actions CI (lint, test matrix, build, strict docs, gitleaks) | Configured — runs on push/PR |
| GitHub Pages deploy workflow | Configured — runs after green CI on `main` |
| Live Google API suite (11 tests) | Implemented — **manual** workflow; not a CI gate |
| PyPI publication (`google-drive-model`) | Automated from signed-off `v*` tags through trusted publishing |
| Public docs site (`yotammanor.github.io/gdm`) | Deployed after green `main` CI |
| OAuth client for distributed users | Application responsibility — secrets are intentionally not bundled |

Every acceptance check below is covered by unit tests, and the opt-in live
Google Drive suite is run against a dedicated root before release.

## 1. Collection model and field mapping

- [x] Add `storage="collection"` and keep `storage="file"` as an alias.
- [x] Add a `collection=` binding that resolves once to an immutable folder ID.
- [x] Extend `Field()` with explicit storage targets:
  - `drive=` for built-in Drive metadata;
  - `metadata=` for the collection metadata table;
  - `property=` and `app_property=` for deliberately constrained Drive custom
    properties.
- [x] Provide a standard read-only Drive identity/metadata mixin without requiring
  applications to inherit every Google field.
- [x] Split materialized state into Drive metadata version, custom metadata
  version, and lazy native content reference.

Acceptance:

- [x] A model maps three fields from an existing image, Doc, and Slide without
  downloading or altering any body (unit + live tests).
- [x] Partial mapping preserves every unmodeled Drive field and metadata column.
- [x] Duplicate filenames coexist; APIs that require one match raise an ambiguity
  error containing all candidate IDs.

## 2. `Collection[T]` API

- [x] Implement `client.collection(Model)` and explicit binding by folder ID.
- [x] Support typed iteration, `get(file_id)`, `find_by_name`, metadata queries,
  MIME selectors, bounded scans, and raw inventory of unmodeled children.
- [x] Support `create_native`, `upload`, `adopt` (with `move=True` or
  `shortcut=True`), `detach`, `move`, `trash`, and separately named permanent
  deletion.
- [x] Model direct children by default and expose nested folders through
  `subcollection`; recursion must be opt-in.
- [x] Preserve shortcut identity and target metadata.

Acceptance:

- [x] Live tests cover folders, nested folders, duplicate names, shortcuts,
  explicit moves, detach/trash semantics, and MIME-filtered queries.
- [x] No collection operation silently moves or deletes an existing object.

## 3. Native content handles

- [x] Add a lazy `ContentHandle` to collection objects.
- [x] Blob adapters: stream download, resumable upload, and checksums.
- [x] Workspace adapters:
  - Docs API structured get and `batchUpdate`;
  - Slides API presentation get and `batchUpdate`;
  - Sheets API access and optional relational binding.
- [x] Add explicit export for portable PDF/text/Office representations.
- [x] Expose authenticated raw API services for operations outside GDM's helpers.
- [x] Never load content during ordinary model listing, validation, or metadata
  query execution.

Acceptance:

- [x] Request-mock tests prove metadata queries make no media/Docs/Slides calls.
- [x] Live tests create a native Doc and Slide, update each through its native API,
  export them, and verify their Drive MIME types never change.
- [x] Live image tests round-trip bytes only after an explicit content call.

## 4. Collection metadata plane

- [x] Create one metadata table per collection, keyed by Drive file ID, under the
  root project's `.gdm/collections/` metadata area.
- [x] Keep built-in Drive metadata in Drive; keep application metadata in targeted
  Sheet cells. A JSON sidecar adapter remains available without Sheets.
- [x] Mirror only bounded routing/index hints to `appProperties`.
- [x] Reconcile orphan rows, missing rows, externally edited rows, moved files,
  and files added directly in Drive.
- [x] Make the metadata plane selectable and replaceable through a backend
  protocol.

Acceptance:

- [x] Directly adding a file to the folder makes it discoverable with default
  metadata.
- [x] Directly editing a metadata Sheet is validated on the next read.
- [x] Removing a row never deletes the native object; deleting an object reports
  and optionally repairs the orphan row.

## 5. Sessions, relationships, migrations, and indexes

- [x] Route collection and row-table models through one session.
- [x] Keep collection object identity as Drive file ID; use it naturally as a
  foreign key from Sheet rows and other collection metadata.
- [x] Batch relationship prefetch by ID without server-side join claims.
- [x] Extend schema snapshots/drift reports to distinguish Drive fields, metadata
  fields, and content capabilities.
- [x] Add additive collection metadata migrations; content and moves require
  explicit operations and destructive approval.
- [x] Key sidecar indexes by Drive ID and detect stale metadata/content
  fingerprints independently.

Acceptance:

- [x] Mixed collection/Sheet relationships work in memory and live tests.
- [x] Migration dry runs contain no Drive write calls.
- [x] Applying a metadata migration leaves native file versions/content hashes
  unchanged except where Google itself updates metadata versions.

## 6. Explorer, inference, and CLI

- [x] Make explorer report folder collections, MIME distributions, nested
  boundaries, duplicate names, shortcuts, and likely metadata fields.
- [x] Infer a collection model from Drive metadata plus optional metadata-table
  samples without reading every body.
- [x] Offer explicit content sampling only for supported codecs.
- [x] Center CLI workflows on `gdm explore`, `gdm infer collection`, `gdm adopt`,
  `gdm doctor`, and collection-aware migration/index commands.

Acceptance:

- [x] A user can point GDM at a pre-existing mixed folder and generate an
  importable partial model and binding proposal without mutations.
- [x] CLI output clearly separates Drive metadata, GDM metadata, and native
  content capabilities.

## 7. Documentation and release audit

- [x] Rewrite the tutorial and README quickstart around an existing mixed Drive
  folder; keep standalone Sheet tables as the relational tutorial.
- [x] Include native Docs, Slides, images, PDFs, shortcuts, duplicate names,
  permissions, shared drives, and multi-root projects.
- [x] Record the final working CLI demo from actual commands; keep the `.cast` and
  regeneration script (`docs/assets/regenerate-demo.sh`).
- [x] Pass Ruff, formatting, ty, pytest with the configured coverage threshold,
  package build, strict docs build, and secret scans on CI for the release tag.
- [x] Run the live suite against the dedicated OAuth account and verify cleanup.
- [x] Push to `main`, enable GitHub Pages (GitHub Actions source), verify public URL.
- [x] Publish `google-drive-model` to PyPI.
- [x] Ship a full example application exercised by CI without credentials
  (`examples/fastapi_assets`).

Work beyond this plan is tracked as the [roadmap](https://github.com/yotammanor/gdm#roadmap)
in the README.
