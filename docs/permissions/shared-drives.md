# Shared drives

GDM passes **`supportsAllDrives=True`** (default on `GDM.open`) and corresponding list
flags on relevant Drive API calls so projects can live in **shared drives**.

## Recommendations

| Topic | Guidance |
| --- | --- |
| Project root | Place `.gdm/` under a shared drive folder with stable membership |
| Service accounts | Share the root folder with the SA email |
| Changes API | Use `client.changes` polling primitives for incremental sync |

## Listing and discovery

```console
gdm explore --root-id SHARED_DRIVE_FOLDER_ID
```

```python
client = GDM.open(
    root_folder_id="SHARED_DRIVE_FOLDER_ID",
    shared_drive_id="SHARED_DRIVE_ID",  # when required by your layout
)
report = client.explore()
```

Exploration respects credentials visible to the authenticated principal.

## Limitations

!!! platform-limit "Drive quotas and policies"
    Shared drives inherit organization policies (download restrictions, sharing limits).
    GDM surfaces capability flags but cannot override domain admin rules.

## Related

- [Permissions overview](index.md)
- [Multi-tenancy](multi-tenancy.md)
- [Design: permissions](../design.md#decision-9-permissions)
