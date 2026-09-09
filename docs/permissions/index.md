# Permissions

Google Drive and shared-drive **ACLs are authoritative**. GDM inspects capabilities and
offers convenience helpers—it does **not** implement row-level security.

## Capability inspection

```python
from gdm import GDM

client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
caps = client.permissions.get_capabilities("FILE_OR_ROOT_ID")
assert caps.can_edit
```

Reported capabilities include `can_edit`, `can_share`, `can_copy`, and `can_download`.

## Sharing helpers

```python
from gdm.permissions import PermissionRole, PermissionType

client.permissions.share(
    "FILE_ID",
    role=PermissionRole.WRITER,
    type=PermissionType.USER,
    email_address="teammate@example.com",
)
client.permissions.unshare("FILE_ID", permission_id="PERMISSION_ID")
```

Prefer explicit Drive sharing policies for production systems.

## Read-only sessions

```python
client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
with client.session(read_only=True) as session:
    ...  # mutating calls raise ReadOnlySessionError
```

`PermissionsManager.ensure_writable(file_id)` raises `PermissionDeniedError` before mutating calls when the caller lacks edit capability.

There are no per-model write guards in v0.1.0—use `session(read_only=True)` or capability checks before mutations.

## Scopes

| Scope class | Typical use |
| --- | --- |
| `drive.file` | GDM-created or user-opened files |
| `drive` / `drive.readonly` | Explore/adopt existing content |

Distributed applications using restricted scopes may require
[Google verification](https://developers.google.com/workspace/guides/configure-oauth-consent).

## Row-level access

!!! warning "No row-level security"
    Worksheets do not enforce per-row audiences. Separate sensitive rows into different
    files or folders with distinct ACLs. Protected ranges are an **editing aid**, not
    authorization.

## Service accounts

Roots must be **shared** with the service account. `client.permissions.service_account_root_hint()` explains common misconfigurations.

## Related

- [Authentication](../auth.md)
- [Shared drives](shared-drives.md)
- [Multi-tenancy](multi-tenancy.md)
