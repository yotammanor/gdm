# Indexes

`Field(index=True)` enables an optional **sidecar index** at:

```text
<local-project>/.gdm/indexes/<model>/<field>.json
```

Collection indexes key by **Drive file ID**. Sheet indexes key by row primary key.
Indexes are machine-local, rebuildable caches; authoritative project bindings and
collection metadata remain in Drive.

## Purpose

An index maps canonical, type-tagged values to logical record IDs with a **source
fingerprint**. It accelerates lookups and unique checks—it is **never the source of
truth**.

## Staleness

Direct edits in Drive, metadata Sheets, or worksheets can stale an index. On read GDM
verifies the fingerprint and may rebuild, fall back to a bounded scan, or raise per policy.
Sheet indexes use the spreadsheet's Drive version for a cheap validation check.
Collection folders do not receive a new Drive version when a child changes, so collection
index validation hashes a metadata-only direct-child inventory. It never downloads native
bodies, but it still incurs a paginated Drive listing; the index then avoids full metadata
materialization and narrows object reads.

Configure the local index directory when opening a client:

```python
client = GDM.open(
    root_folder_id="YOUR_ROOT_FOLDER_ID",
    indexes_dir=".gdm/indexes",
)
```

```console
gdm --project-dir . index rebuild --model CampaignAsset --field campaign \
  --resource-id FOLDER_ID
```

Indexes update on GDM writes and can be rebuilt explicitly. The CLI reads live
collection metadata or worksheet rows. Content indexes require a programmatic extractor;
the CLI rejects `--content-indexed` in apply mode rather than downloading bodies implicitly.

## Unique indexes

Unique indexes are checked **optimistically**. They are not a security boundary or a
cross-client uniqueness guarantee—two writers can still race.

## When to index

| Pattern | Recommendation |
| --- | --- |
| Frequent lookup by slug or campaign code | `index=True` on metadata field |
| Rare full-table scans | Skip index; use bounded queries |
| Large collections | Run `gdm doctor` first |

## Related

- [Query data](../tutorial/sheets.md#3-query-metadata-and-rows)
- [Performance envelope](../design.md#practical-scale-envelope)
