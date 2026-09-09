# Define a model

This content moved to the split tutorials:

- **Drive collections:** [tutorial/collection.md](collection.md) and [storage/collections.md](../storage/collections.md)
- **Sheet tables:** [tutorial/sheets.md](sheets.md) and [storage/sheets.md](../storage/sheets.md)

Quick reference — class keywords on `GDMModel`:

| Keyword | Purpose |
| --- | --- |
| `storage="collection"` | Drive folder collection (alias: `"file"`) |
| `storage="sheet"` | Worksheet rows |
| `collection="path/hint"` | Collection discovery hint (persists as folder ID) |
| `mapping` | `partial` (default), `permissive`, or `strict` |
| `codec` | Opt-in `json` / `text` / `bytes` / `yaml` for structured file collections |
| `mime` | MIME selector(s) for collection members |

Field targets: `drive=`, `metadata=`, sheet `source=`, `property=`, `app_property=`.

See [API reference](../api/index.md).
