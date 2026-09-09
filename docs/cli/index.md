# CLI reference

Entry point: **`gdm`** (`gdm.cli:app` via Typer).

```console
gdm --help
```

Global options apply before subcommands:

| Option | Purpose |
| --- | --- |
| `--project-dir PATH` | Local directory with `.gdm/` metadata (`GDM_PROJECT_DIR`) |
| `--dry-run` / `--apply` | Default inspection-only; `--apply` enables mutations |

## Commands

### `gdm auth login`

Installed-user OAuth. Tokens stored outside the project.

```console
gdm auth login
gdm auth login --scopes drive.readonly,spreadsheets   # exploration-only
```

Default scopes are `drive,spreadsheets` (required to discover and edit existing Drive content). Requires the `oauth` extra and `GDM_CLIENT_SECRETS` or `--client-secrets`.

!!! note "Implemented today"
    `gdm auth login` is the supported auth subcommand. Logout/status helpers are not shipped yet—delete the token file locally to sign out.

### `gdm init`

Create local `.gdm/` scaffolding.

```console
gdm --project-dir . init --root-id YOUR_ROOT_FOLDER_ID --name my-app
gdm --project-dir . --apply init --root-id ID --remote   # also create remote project
```

### `gdm explore`

Non-mutating folder inventory (metadata only).

```console
gdm explore --root-id YOUR_ROOT_FOLDER_ID
gdm explore --root-id ID --json
```

### `gdm infer`

| Subcommand | Description |
| --- | --- |
| `infer collection` | Model from Drive folder metadata (no content reads) |
| `infer file` | Model from local JSON/text/CSV sample |

```console
gdm infer collection --folder-id FOLDER_ID --model-name CampaignAsset
gdm infer file sample.json --model-name SampleDoc --output models/sample.py
```

Review inferred types and MIME selectors before binding; generated field
targets already use the public `source=`, `drive=`, and `metadata=` arguments
(see [Model inference](../exploration/inference.md)).

### `gdm adopt`

Bind a folder as a collection model (dry-run by default).

```console
gdm --project-dir . adopt --model-name CampaignAsset --folder-id FOLDER_ID
gdm --project-dir . --apply adopt --model-name CampaignAsset --folder-id FOLDER_ID
gdm --project-dir . --apply adopt --model-name CampaignAsset --folder-id FOLDER_ID --metadata-backend json
gdm --project-dir . --apply adopt --model-name CampaignAsset --folder-id FOLDER_ID --adoption-mode
```

Apply mode opens the Drive project named by the local manifest, persists the
folder binding, and creates the selected Sheets or JSON metadata store. Existing
folder members and native bodies are not retagged, moved, or rewritten.
By default the folder must be under the project root and cannot be tagged for
another project. `--adoption-mode` explicitly bypasses those checks for a
consolidation workflow.

### `gdm doctor`

Metadata reconciliation and schema/index diagnostics.

```console
gdm --project-dir . doctor
gdm --project-dir . doctor --model CampaignAsset --folder-id FOLDER_ID --json
```

### `gdm migrate`

Plan and apply schema migrations via `gdm.migrations`.

```console
gdm --project-dir . migrate plan --model Task --resource-id SPREADSHEET_OR_FOLDER_ID
gdm --project-dir . --apply migrate apply --model Task --resource-id ID
gdm --project-dir . migrate rollback --model Task --resource-id ID
gdm --project-dir . --apply migrate rollback --model Task --resource-id ID
```

`migrate apply` connects to the production Google target from `.gdm/manifest.json` (ADC or stored OAuth) unless tests inject `migration_target_factory`. Destructive operations require `--allow-destructive` on apply.

`migrate rollback` reads the apply checkpoint and only runs reverse operations
declared by that migration. It is also dry-run by default; non-reversible or
destructive reverse plans are blocked before any write.

### `gdm index rebuild`

Rebuild sidecar indexes from the bound live collection or worksheet.

```console
gdm --project-dir . index rebuild --model Task --field title --resource-id ID
```

The command reads metadata/rows but never native file bodies. Content-indexed
rebuilds require an explicit programmatic extractor and are rejected by the CLI.

## Terminal demo

An **actual local CLI recording** (no live OAuth or Drive mutations) is committed as
[`docs/assets/demo.cast`](../assets/demo.cast). Regenerate:

```console
bash docs/assets/regenerate-demo.sh
```

See [Terminal demo](../assets/terminal-demo.md).

## Related

- [Installation](../installation.md)
- [Authentication](../auth.md)
- [Explore your Drive](../exploration/index.md)
