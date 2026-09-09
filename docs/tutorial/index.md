# Tutorial

This tutorial has two first-class paths:

1. **[Drive collections](collection.md)** — adopt a mixed folder of Docs, Slides, images, PDFs, shortcuts, and nested subfolders.
2. **[Sheet tables](sheets.md)** — bind a worksheet for relational rows, foreign keys, and prefetch.

!!! tip "Runnable examples"
    Example scripts live in [`docs_src/`](https://github.com/yotammanor/gdm/tree/main/docs_src). Snippets use
    [`--8<--` snippets](https://facelessuser.github.io/pymdown-extensions/extensions/snippets/)
    from those files.

## Start with collections

Most teams already have a useful Drive folder before they add GDM. The collection tutorial covers:

- connecting with [`GDM.open()`](../api/index.md#quick-reference)
- binding a folder with `client.bind_collection(...)`
- listing, filtering, adopting, and moving members
- lazy native content via `object.content`
- metadata queries through `Session` / `select()` that **never fetch bodies**

[:octicons-arrow-right-24: Drive collections tutorial](collection.md)

## Sheet tables

When your source of truth is a spreadsheet, follow the sheet tutorial for model definition, queries, CRUD, and relationships.

[:octicons-arrow-right-24: Sheet tables tutorial](sheets.md)

## Initialize project metadata

Pick **one Drive folder** as the anchor for this application. Different apps and environments should use different roots—see [Multi-tenancy](../permissions/multi-tenancy.md).

```console
gdm auth login
gdm --project-dir . --apply init --root-id YOUR_ROOT_FOLDER_ID --name my-app --remote
```

This creates the shared Drive project metadata and local tooling directories.
Omit `--apply --remote` only when you intentionally want local scaffolding
without opening the project yet:

```text
<local-project>/
└── .gdm/
    ├── manifest.json
    ├── registry.json
    ├── schemas/
    ├── migrations/
    └── indexes/
```

The authoritative Drive metadata is:

```text
<Drive root>/
└── .gdm/
    ├── manifest.json
    ├── registry.json
    └── collections/
```

## Platform expectations

!!! warning "Not a database transaction"
    `Session.commit()` validates locally, batches writes, and raises
    [`PartialCommitError`](../consistency/errors.md#partialcommiterror) if a later batch
    fails. It does **not** roll back earlier successful Drive API calls.

## Design background

Implementation acceptance criteria and architecture decisions:

- [Design decisions](../design.md)
- [Implementation plan](../implementation-plan.md)
