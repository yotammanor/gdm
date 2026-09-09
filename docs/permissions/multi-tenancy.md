# Multi-tenancy

Isolate applications by anchoring each project to a distinct **Drive root folder** via
[`GDM.open(root_folder_id=…)`](../api/index.md#quick-reference).

## Project layout

```text
<root>/
└── .gdm/
    ├── manifest.json      # project UUID, format version
    ├── registry.json      # model → resource IDs
    └── collections/       # per-collection metadata tables
```

The manifest UUID is stable. The root folder ID is chosen at `gdm init` / first connect.

## Cross-project isolation

`GDM.open(..., adoption_mode=True)` allows resources outside the selected root or
tagged for another project during consolidation or migration. Default clients
raise [`ForeignProjectError`](../consistency/errors.md) when root ancestry or app
property routing keys disagree.

Use adoption deliberately—never as a default.

## Same account, many roots

Different apps, tenants, or environments may:

- share one Google account or service account
- share a parent folder in Drive
- use **different root folder IDs**

Local token and cache paths remain user-specific and are never uploaded to Drive.

## Credentials vs project data

| Stored locally | Stored in Drive `.gdm/` |
| --- | --- |
| OAuth tokens | Manifest and registry |
| Schema snapshots and migration checkpoints | Collection metadata Sheets/JSON |
| Rebuildable index and API caches | — |

Never commit tokens. Drive project metadata is shared across machines; local
tooling state should be committed only when your application intentionally
versions its schema/migration files, while generated indexes stay untracked.

## Related

- [Authentication](../auth.md)
- [Permissions overview](index.md)
- [Design: root-scoped projects](../design.md#decision-5-root-scoped-projects-and-multi-tenancy)
