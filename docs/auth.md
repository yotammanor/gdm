# Authentication

GDM uses official Google client libraries (`google-auth`, optional
`google-auth-oauthlib`). Tokens are stored in **platform user-data directories**—never
in the Drive `.gdm/` project folder.

## Installed-user OAuth (interactive)

Recommended for local development and notebooks.

```console
gdm auth login
```

The default requests `drive,spreadsheets`, which is required to discover and
edit arbitrary existing Drive content.

For an exploration-only token:

```console
gdm auth login --scopes drive.readonly,spreadsheets \
  --client-secrets /path/to/client_secret.json
```

Optional: set `GDM_CLIENT_SECRETS` to avoid passing `--client-secrets` each time.

Subsequent [`GDM.open()`](api/index.md#quick-reference) calls reuse stored credentials.

## Scopes

| Scope alias | Full scope | When needed |
| --- | --- | --- |
| `drive.file` | `…/auth/drive.file` | GDM-created or user-opened files |
| `drive.readonly` | `…/auth/drive.readonly` | Explore/infer existing folders read-only |
| `drive` | `…/auth/drive` | Adopt/move arbitrary existing content |
| `spreadsheets` | `…/auth/spreadsheets` | Sheet rows and collection metadata tables |

Choose the narrowest scope compatible with your application. The default local
developer flow uses the broader Drive scope because `drive.file` cannot discover
arbitrary pre-existing content. Distributed apps using restricted scopes may
need [Google verification](https://developers.google.com/workspace/guides/configure-oauth-consent).

## Service accounts (automation)

Service accounts work after the **project root folder is shared** with the service
account email.

```console
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
```

```python
from gdm import GDM

client = GDM.open(
    root_folder_id="YOUR_ROOT_FOLDER_ID",
    service_account_file="/path/to/key.json",
)
print(client.permissions.service_account_root_hint())
```

!!! platform-limit "Service account limits"
    Service accounts have no My Drive quota of their own. The Drive root must live in a
    shared drive or user drive folder explicitly shared with the account.

## Application Default Credentials

When `GOOGLE_APPLICATION_CREDENTIALS` or gcloud user credentials are configured:

```python
client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID", use_adc=True)
```

The CLI uses ADC for remote operations when `--apply` is set (for example `gdm init --remote`).

## Credential storage

| Location | Contents |
| --- | --- |
| Local user data dir (`~/.config/gdm/token.json` or platform equivalent) | OAuth tokens |
| Drive `.gdm/` | Project manifest, registry, collection metadata—**no secrets** |
| Local project `.gdm/` | Schema snapshots, migration checkpoints, rebuildable indexes |

Never commit tokens or service-account keys. Live tests use opt-in markers—see [Testing](testing.md).

## Read-only sessions

Prevent accidental writes:

```python
with client.session(read_only=True) as session:
    rows = session.exec(select(Task)).all()
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| OAuth `403 access_denied` | Set the consent audience to **External / Testing** and add the account as a test user |
| API `403` insufficient permissions | Enable Drive, Sheets, Docs, and Slides APIs; verify scopes or share the root with the service account |
| Token refresh errors | Re-run `gdm auth login`; confirm clock sync |
| Shared drive not listed | Pass `supports_all_drives=True` (default on `GDM.open`)—see [Shared drives](permissions/shared-drives.md) |

## Next

[Drive collections tutorial](tutorial/collection.md) · [Permissions overview](permissions/index.md)
