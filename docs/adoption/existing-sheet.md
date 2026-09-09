# Adopt an existing spreadsheet

GDM is designed for spreadsheets **your team already maintains**. Adoption binds a model
to a worksheet without rewriting the sheet.

## Workflow

1. **Explore** — list spreadsheets under the project root
2. **Infer** — suggest field types from headers (optional local CSV sample via `infer file`)
3. **Bind** — register spreadsheet + worksheet with `client.bind(..., kind=ResourceKind.SPREADSHEET)`
4. **Validate** — run reads; fix diagnostics before writes

```console
gdm explore --root-id YOUR_ROOT_FOLDER_ID
gdm infer file sample_rows.csv --model-name Inventory
```

The `gdm adopt` command is collection-folder specific. Bind a worksheet directly
in Python with the spreadsheet ID:

```python
from gdm import ResourceKind

client.bind(
    Inventory,
    resource_id="SPREADSHEET_ID",
    kind=ResourceKind.SPREADSHEET,
    worksheet="Inventory",
)
```

## Example model

```python
--8<-- "models_inventory.py"
```

### Preserve unknown columns

With `mapping="partial"` (default), updates touch **only mapped cells**. Extra
columns your team added remain untouched.

### Primary key strategies

| Strategy | When to use |
| --- | --- |
| Existing unique column (`sku` above) | Stable business key already in the sheet |
| `_gdm_id` system column | No natural key; GDM adds UUID column via migration |
| Drive file ID column | Link rows to collection objects |

Adding `_gdm_id`, `_gdm_version`, or `_gdm_updated_at` to a **non-empty**
worksheet requires an explicit [migration](../migrations/index.md). An **empty**
worksheet gets declared field headers plus system columns on the first
`Session` create—see [Sheets as tables](../storage/sheets.md).

## Discovery vs immutable IDs

Bindings may start from a path or name. GDM **persists the resolved resource ID** before
writes because Drive allows duplicate names.

Ambiguous discovery is always an error.

## Permissions

Reading arbitrary existing content may require broader OAuth scopes than `drive.file`.
See [Authentication](../auth.md) and [Permissions](../permissions/index.md).

## Related

- [Sheet tables tutorial](../tutorial/sheets.md)
- [Sheets as tables](../storage/sheets.md)
- [Model inference](../exploration/inference.md)
