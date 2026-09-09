# Drive collections

`storage="collection"` (alias: `storage="file"`) maps a Pydantic model to **direct children** of one Drive folder.

Native Google Docs, Slides, images, PDFs, and uploaded binaries stay **native and lazy**. GDM adds a typed metadata plane without replacing file bodies.

## Model layout

```python
from gdm import DriveObjectMixin, Field, GDMModel


class CampaignAsset(
    GDMModel,
    DriveObjectMixin,
    storage="collection",
    collection="Campaigns/Launch",
    mapping="partial",
    mime=("application/vnd.google-apps.document", "image/*"),
):
    campaign: str | None = Field(default=None, metadata="campaign")
    approved: bool = Field(default=False, metadata="approved")
```

| Target | Storage |
| --- | --- |
| `Field(drive=...)` | Built-in Drive file fields (`name`, `description`, `starred`, …) |
| `Field(metadata=...)` | Collection metadata Sheet (keyed by Drive file ID) |
| `Field(property=...)` / `Field(app_property=...)` | Small Drive custom properties (bounded—never full records) |

Read-only Drive fields (`id`, `mimeType`, timestamps, checksums) materialize on read but are rejected in update masks.

## Metadata plane

Each collection gets a metadata table under:

```text
<root>/.gdm/collections/<collection-id>/
```

Default representation is a **Google Sheet** (requires Sheets API). A JSON sidecar adapter is available when Sheets is disabled. Only routing hints (`gdm_project`, `gdm_model`, `gdm_schema`) mirror to private `appProperties`.

## DriveCollection API

Obtain a collection from a connected client:

```python
client.bind_collection(CampaignAsset, path="Campaigns/Launch")
assets = client.collection(CampaignAsset)
```

Path discovery is optional. `folder_id="..."` binds directly; either route stores
the immutable Drive folder ID in the project registry before writes.

| Method | Description |
| --- | --- |
| `list()` | Typed direct children matching MIME selectors |
| `get(file_id)` | One member by Drive ID |
| `find_by_name(name)` / `one_by_name(name)` | Name lookup (ambiguous → error) |
| `where_mime(pattern)` | Filter by MIME glob |
| `inventory()` | All direct children, including unmodeled |
| `subcollection(folder_id)` / `iter_recursive()` | Opt-in nesting |
| `create_native(name, kind, metadata=…)` | New Google Doc/Slide/Sheet/etc. |
| `upload(name, data, mime_type=…)` | Binary upload |
| `adopt` / `move` / `detach` | Explicit membership changes |
| `trash` / `delete_permanently` | Trash vs permanent deletion |
| `reconcile_metadata()` | Orphan/missing metadata rows |

Instance API after materialization:

- `instance.content` — lazy [`ContentHandle`](../api/index.md#quick-reference)
- `instance.gdm_record_key()` — Drive file ID

## Structured file codecs (opt-in)

For greenfield JSON/text/byte/YAML files in a folder—not native Docs/Slides—set `codec`:

| Codec | Body | Model shape |
| --- | --- | --- |
| `json` | JSON object | Multi-field mapping; unknown keys preserved in `partial`/`permissive` |
| `text` | UTF-8 text | Exactly one `str` body field (excluding primary key) |
| `bytes` | Opaque binary | Exactly one `bytes` body field; MIME from `mime=` or `application/octet-stream` |
| `yaml` | YAML mapping | Same semantics as `json`; requires the `yaml` extra (`google-drive-model[yaml]` on PyPI, or `uv pip install -e ".[yaml]"` from source) |

<!-- fmt:off -->
```python
--8<-- "models_release_note.py"
```
<!-- fmt:on -->

Codec models serialize declared fields into the file body on write. GDM stores routing keys in private `appProperties` (`gdm_project`, `gdm_model`, `gdm_id`, optional `gdm_schema`) and scans by those keys. Updates merge routing properties without dropping unrelated `appProperties`. Adopting an existing Google Doc never uses `codec=`.

## Queries

Metadata predicates use **bounded scans** (default `scan_limit`) with local
evaluation, or **verified sidecar indexes** when `Field(index=True)` is set and
the `where` clause matches. They **never** download file bodies.

```python
with client.session(scan_limit=500) as session:
    rows = session.exec(
        select(CampaignAsset).where(CampaignAsset.approved == True)  # noqa: E712
    ).all()
```

Backends report `supports_metadata_pushdown=False`; GDM does not claim Drive `q`
predicate pushdown in v0.1.0.

Content search requires explicit content adapters or Drive full-text APIs—not ORM field predicates.

## Related

- [Drive collections tutorial](../tutorial/collection.md)
- [Adopt Drive folders](../adoption/drive-collections.md)
- [Design: collections](../design.md#decision-3-collections-and-native-drive-objects-are-the-center)
