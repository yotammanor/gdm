# Introduction

Google Drive Model (**GDM**) lets you treat **existing Drive folders** and **spreadsheets** as typed,
validated data sources using **Pydantic**—with a session and query style inspired by
[SQLModel](https://sqlmodel.tiangolo.com/), but without SQLAlchemy or fake SQL semantics.

## Problems GDM solves

**Teams already store data in Drive.** Launch folders mix Google Docs, slide decks, creative assets, and PDFs. Spreadsheets remain the system of record long before anyone adds a database. GDM meets that data where it lives.

**Human edits happen.** GDM validates at the boundary: invalid cells, metadata rows, or structured edits produce precise diagnostics instead of silent coercion.

**Adoption can be incremental.** Bind one folder or worksheet, map a subset of fields, and ignore the rest until you are ready.

## Core concepts

| Concept | Role |
| --- | --- |
| [`GDM`](api/index.md#quick-reference) | Connect to a root-scoped project; bind resources; open collections and sessions |
| [`DriveCollection[T]`](api/index.md#quick-reference) | Typed view of **direct children** in one Drive folder |
| [`DriveObject`](api/index.md#quick-reference) | Base class for collection models; adds Drive identity fields |
| [`GDMModel`](api/index.md#quick-reference) | Pydantic model with `storage`, `collection`, and `mapping` class keywords |
| [`Field()`](api/index.md#quick-reference) | `drive=`, `metadata=`, sheet `source=`, indexes, foreign keys |
| [`Session`](api/index.md#quick-reference) | Unit-of-work for sheet rows and collection metadata queries |
| [`select()`](api/index.md#quick-reference) | Builds a backend-independent expression tree |
| **Project root** | One Drive folder anchors the shared manifest, registry, and collection metadata in its `.gdm/` folder |
| **Native content** | Lazy `object.content` handle—never loaded by metadata queries |

## Two first-class storage shapes

=== "Drive collection"

    One model maps to **direct children** of a Drive folder. Identity is the immutable
    Drive file ID; names are presentation metadata. Application fields live in a metadata
    Sheet under `.gdm/collections/`. See [Drive collections](storage/collections.md).

    ```python
    class Asset(DriveObject, collection="Assets"):
        campaign: str | None = Field(default=None, metadata="campaign")
    ```

=== "Sheet table"

    One model maps to **one worksheet**. Headers are field source names; rows are
    instances. See [Sheets as tables](storage/sheets.md).

    ```python
    class Task(GDMModel, storage="sheet", mapping="partial"):
        title: str
    ```

`storage="file"` remains a compatibility alias for `storage="collection"`. Use `codec="json"` (or `text` / `bytes`) only for greenfield structured file collections—not when adopting native Google Docs or Slides.

Drive `.gdm/` contains shared project state. A separate local
`<project>/.gdm/` contains schema snapshots, migration checkpoints, and
rebuildable indexes; see [Multi-tenancy](permissions/multi-tenancy.md).

## Sync API first

The public API is **synchronous**—suitable for scripts, notebooks, and CLI tools. There is no pretend-native `async` layer over blocking Google clients.

## When to choose something else

GDM optimizes for **human-scale and team-scale** data: thousands of files per collection, tens of thousands of sheet rows with batching and indexes.

Consider BigQuery, Cloud SQL, or a document database when you need server-side joins at high volume, strict cross-record transactions, or sub-millisecond query latency.

Run [`gdm doctor`](cli/index.md) to surface scale, drift, and metadata reconciliation issues early.

## Read next

- [Installation](installation.md)
- [Drive collections tutorial](tutorial/collection.md)
- [Design decisions](design.md)
