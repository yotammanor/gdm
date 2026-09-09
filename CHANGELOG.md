# Changelog

All notable changes to **google-drive-model** (import and CLI: `gdm`) are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-09-09

First public alpha release.

### Added

- `GDMModel`, `Field()`, `Session`, `select()`, and `DriveCollection[T]` for
  Drive folders and Sheets.
- Progressive mapping modes (`partial`, `permissive`, `strict`).
- Lazy native content handles for Docs, Slides, Sheets, and binary files, with
  Docs/Slides revision preconditions and best-effort Drive/Sheets conflict checks.
- Root-scoped projects with Drive-hosted manifest, registry, and collection
  metadata plus local schema snapshots, migration checkpoints, and indexes.
- Sidecar indexes with fingerprint verification, unique checks, write
  maintenance, and `gdm index rebuild`.
- Forward migrations with resumable checkpoints and declared rollback operations.
- CLI commands for authentication, initialization, exploration, inference,
  adoption, diagnostics, migrations, and index maintenance.
- In-memory test helpers for offline applications and documentation examples.
- GitHub Actions for secret scanning, Ruff, ty, Python 3.11–3.14 tests, package
  verification, strict documentation, Pages deployment, and trusted publishing.

### Documentation

- MkDocs Material documentation site and API reference.
- Runnable `docs_src/` examples with `GDM_USE_FAKE=1` for offline metadata queries.
- Terminal demo cast from actual local CLI output (`docs/assets/regenerate-demo.sh`).

### Known alpha limitations

- **No ACID transactions** across Drive files or sheets; `Session.commit()` coordinates writes sequentially, not atomically.
- **No Drive query pushdown** in v0.1.0; metadata queries use bounded scans or optional sidecar indexes.
- **Client-side joins** and relationship prefetch — no server-side query engine.
- **Drive permissions** are the security boundary; GDM does not add row-level security.
- **Sync API only** — no async transport layer in v0.1.0.
- **Scale**: tuned for team-scale folders and sheets; use `gdm doctor` for drift/scale warnings.
- **Alpha API**: pin versions in production integrations until v1.0.

[Unreleased]: https://github.com/yotammanor/gdm/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/yotammanor/gdm/releases/tag/v0.1.0
