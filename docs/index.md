# Google Drive Model

**GDM** brings Pydantic models and a small, SQLModel-inspired API to **Google Drive** and **Google Sheets**.

It is built for drives that already contain useful, human-edited data—not only for greenfield storage. The primary workflow adopts a **mixed Drive folder** as a typed `DriveCollection[T]` while keeping native Docs, Slides, images, and PDFs lazy until you explicitly read or mutate content.

<div class="grid cards" markdown>

-   :material-folder-google-drive:{ .lg .middle } __Drive collections__

    ---

    Map an existing folder of Docs, Slides, images, PDFs, and shortcuts. Metadata queries never fetch bodies.

    [:octicons-arrow-right-24: Collections tutorial](tutorial/collection.md)

-   :material-table:{ .lg .middle } __Sheet tables__

    ---

    Bind one worksheet, preserve unknown columns, validate at the boundary.

    [:octicons-arrow-right-24: Sheet tutorial](tutorial/sheets.md)

-   :material-shield-alert:{ .lg .middle } __Honest limits__

    ---

    No fake SQL transactions. Platform constraints are part of the contract.

    [:octicons-arrow-right-24: Consistency](consistency/errors.md)

-   :material-book-open-variant:{ .lg .middle } __Design decisions__

    ---

    Architecture, metadata plane, native content handles, and migrations.

    [:octicons-arrow-right-24: Design](design.md)

</div>

## Install name vs import name

The distribution is **`google-drive-model`** because the PyPI name `gdm` belongs
to another project. After installation you import **`gdm`**:

```python
from gdm import GDM, GDMModel, DriveCollection, Field, Session, select
```

## Minimal example

--8<-- "quickstart_collection.py"

Run metadata queries offline without Google credentials (seeded in-memory backend):

```console
GDM_USE_FAKE=1 python docs_src/quickstart_collection.py
```

This exercises `select()` / `session.exec()` against seeded `CampaignAsset` rows.
Full `client.collection()` listing and content handles require a live Drive bind — see
[Testing](testing.md).

## What GDM is not

!!! warning "Platform limits"
    Google Drive is collaborative storage, not an ACID database. GDM validates and
    coordinates writes, but cannot guarantee cross-file transactions, server-side
    joins, or row-level security. See [Consistency & errors](consistency/errors.md).

## Next steps

1. [Install](installation.md) the package.
2. [Authenticate](auth.md) with OAuth, ADC, or a service account.
3. Follow the [Drive collections tutorial](tutorial/collection.md) or [adopt a sheet](adoption/existing-sheet.md).

## Terminal demo

<p class="terminal-poster">
  <a href="assets/demo.cast">
    <img src="assets/demo.gif" alt="Actual local GDM CLI recording (generated cast file)">
  </a>
</p>

Open the [terminal demo](assets/terminal-demo.md) page for playback and regeneration.
