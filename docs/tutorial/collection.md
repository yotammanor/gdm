# Drive collections

Adopt an **existing mixed Drive folder**—Google Docs, Slides, images, PDFs, shortcuts, duplicate names, and nested subfolders—without converting native files.

## 1. Connect and bind

```python
from gdm import GDM

from models_campaign_asset import CampaignAsset

client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
client.bind_collection(
    CampaignAsset,
    path="Campaigns/Launch",
)
assets = client.collection(CampaignAsset)
```

`bind_collection` resolves the path once and persists the immutable folder ID.
Pass `folder_id=...` instead when you already know it. `client.collection(Model)`
returns a [`DriveCollection`](../api/index.md#quick-reference) scoped to that ID.

## 2. Define the model

<!-- fmt:off -->
```python
--8<-- "models_campaign_asset.py"
```
<!-- fmt:on -->

| Field target | Maps to |
| --- | --- |
| `Field(drive="name")` | Built-in Drive file metadata (via `DriveObjectMixin`) |
| `Field(metadata="campaign")` | Application column in the collection metadata Sheet under `.gdm/collections/` |

Unmodeled direct children remain visible through `assets.inventory()` and are untouched by typed list/get operations.

## 3. Query metadata (no body fetch)

<!-- fmt:off -->
```python
--8<-- "query_basic.py"
```
<!-- fmt:on -->

## 4. List, get, and filter

```python
all_assets = assets.list()  # modeled direct children only
brief = assets.get("FILE_ID")  # by immutable Drive ID
matches = assets.find_by_name("brief.pdf")  # duplicate names → list
one = assets.one_by_name("brief.pdf")  # raises AmbiguousResourceError if >1

images = assets.where_mime("image/*")
pdfs = assets.where_mime("application/pdf")
```

### Collection semantics

| Rule | Behavior |
| --- | --- |
| Identity | Drive file ID (`drive_id` / locator record key) |
| Duplicate names | Allowed; `one_by_name` raises [`AmbiguousResourceError`](../consistency/errors.md) |
| Scope | **Direct children** by default; use `iter_recursive()` or `subcollection()` for nesting |
| Shortcuts | Remain shortcuts; target metadata exposed on inventory items |
| Metadata queries | `Session` / `select()` on metadata fields—**no body download** |
| Session delete | **Trashes** the file (`session.delete()` → Drive trash) |
| Permanent delete | Explicit `assets.delete_permanently(file_id)` only |

## 5. Membership without silent moves

<!-- fmt:off -->
```python
--8<-- "collection_operations.py"
```
<!-- fmt:on -->

| Operation | Effect |
| --- | --- |
| `adopt(id)` | Add metadata for a file already in the folder |
| `adopt(id, move=True)` | Move file into the collection folder |
| `adopt(id, shortcut=True)` | Create a shortcut in the folder |
| `move(id, to_folder_id=…)` | Reparent an existing member into another collection folder |
| `detach(id, to_folder_id=…)` | Reparent out of the collection—**does not delete** |
| `trash(id)` | Move to Drive trash |
| `delete_permanently(id)` | Irreversible Drive deletion |

```python
assets.move("FILE_ID", to_folder_id="ARCHIVE_FOLDER_ID")
```

## 6. Native content stays lazy

<!-- fmt:off -->
```python
--8<-- "collection_content.py"
```
<!-- fmt:on -->

| MIME family | Content API |
| --- | --- |
| Blobs, images, PDFs | `content.download()` / `content.upload(...)` |
| Google Docs | `content.google_doc()` → Docs API `get` / `batchUpdate` |
| Google Slides | `content.google_slides()` → Slides API |
| Google Sheets (as files) | `content.google_sheet()` or bind as `storage="sheet"` table |
| Portable export | `content.export("application/pdf")` — never confused with native mutation |

Merely calling `assets.list()`, `session.exec(select(...))`, or validating metadata **does not** download media or call Docs/Slides APIs.

Explicit content mutations check the Drive snapshot captured when the object was
materialized. Docs and Slides batch updates also use Google's atomic
`requiredRevisionId` write control; pass `expected_revision_id=...` when you
already hold a revision. Drive binary uploads and Sheets have no server-side
revision precondition, so reload after a `ConflictError` and coordinate
single-writer workflows when lost updates are unacceptable.

## 7. Create and upload

```python
doc = assets.create_native("Launch brief", kind="document", metadata={"campaign": "Q1"})
png = assets.upload(
    "hero.png",
    data=open("hero.png", "rb").read(),
    mime_type="image/png",
    metadata={"approved": True},
)
```

## 8. Metadata reconciliation

Humans can add files directly in Drive or edit the metadata Sheet. Reads do not
rewrite either side; reconcile explicitly when you want a drift report:

```python
orphans, missing = assets.reconcile_metadata()
```

**Trash and delete do not remove metadata rows.** `Session.delete()` and
`DriveCollection.trash()` move the native Drive file to trash; the metadata-table
row remains until you reconcile or repair it explicitly. Run
[`gdm doctor`](../cli/index.md) to list orphan metadata rows and other drift.

## Full quickstart script

<!-- fmt:off -->
```python
--8<-- "quickstart_collection.py"
```
<!-- fmt:on -->

## Next

- [Sheet tables tutorial](sheets.md)
- [Storage: collections](../storage/collections.md)
- [Adopt Drive folders](../adoption/drive-collections.md)
