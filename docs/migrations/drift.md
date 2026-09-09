# Schema drift

Drift is the difference among three snapshots:

1. **Code model** — current Pydantic/`GDMModel` definition
2. **Last applied schema** — snapshot stored in `.gdm/schemas/`
3. **Live source** — worksheet headers, collection metadata Sheet, or Drive fields

```console
gdm --project-dir . migrate plan --model Task --resource-id RESOURCE_ID
gdm --project-dir . doctor --model CampaignAsset --folder-id FOLDER_ID
```

## Why three-way?

| Change type | Typical cause |
| --- | --- |
| Code ahead of source | You added a field; migration needs apply |
| Source ahead of code | Teammate added a column; partial mode preserves it |
| Both diverged | Requires explicit resolution or strict rejection |

Collection drift distinguishes **Drive metadata**, **metadata-table columns**, and **content capabilities**—native file versions stay unchanged during metadata-only migrations.

## Mapping mode behavior

| Mode | Unknown source fields |
| --- | --- |
| `partial` | Ignored on read; preserved on write |
| `permissive` | Available on `gdm_extra` |
| `strict` | Validation error |

## Human edits

GDM treats human edits as first-class. Invalid values produce **precise Pydantic
diagnostics** rather than silent fixes.

## Related

- [Migrations overview](index.md)
- [Define a model](../tutorial/model.md)
- [Design decisions](../design.md#decision-6-drift-aware-forward-migrations)
