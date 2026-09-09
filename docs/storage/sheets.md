# Sheets as tables

`storage="sheet"` maps one [`GDMModel`](../api/index.md#quick-reference) to **one worksheet**.

## Layout

| Sheet row | GDM |
| --- | --- |
| Header row | Field `source` names |
| Data rows | Model instances |

## System columns

When GDM manages row identity it may use:

| Column | Purpose |
| --- | --- |
| `_gdm_id` | Stable UUID when no explicit primary key |
| `_gdm_version` | Optimistic version |
| `_gdm_updated_at` | UTC timestamp of latest GDM write |

Existing sheets can nominate an existing unique column as the primary key instead.

When the bound worksheet is **empty**, the first create through `Session` writes
the header row from declared field `source` names plus `_gdm_id`, `_gdm_version`,
and `_gdm_updated_at` when GDM manages row identity.

## Reads and writes

- Reads fetch **bounded ranges** and validate with Pydantic
- Updates target **only mapped cells**
- Sheet `batchUpdate` is atomic **within one request**, not across separate API calls or
  collaborator edits

!!! platform-limit "Collaborative editing"
    A teammate editing the same row in the Sheets UI can race your commit. GDM detects
    many conflicts via [`ConflictError`](../consistency/errors.md#conflicterror) but cannot
    eliminate concurrent human edits.

## Batch sizing

Set `scan_limit` on `GDM.open(...)` or `client.session(scan_limit=...)` to bound
reads, and configure indexes for frequent equality lookups. Run
[`gdm doctor`](../cli/index.md) for scale warnings.

## Unknown columns

Controlled by the `mapping` class keyword:

- **`partial`**: preserve, do not require
- **`permissive`**: expose as `gdm_extra`
- **`strict`**: reject on read

## Related

- [Adopt an existing spreadsheet](../adoption/existing-sheet.md)
- [Relationships](relationships.md)
- [Indexes](../performance/indexes.md)
