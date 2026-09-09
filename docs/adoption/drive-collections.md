# Adopt Drive folders

Adopt a **pre-existing mixed folder**—Docs, Slides, images, PDFs, shortcuts, duplicates, nested subfolders—by binding a collection model without rewriting any native content.

## Workflow

1. **Explore** — inventory MIME distribution and collection candidates (zero content reads)
2. **Infer** — propose a partial model from Drive metadata (+ optional metadata-table samples)
3. **Adopt** — bind folder ID in the registry (dry-run by default)
4. **Validate** — list/get through `DriveCollection`; reconcile metadata with `gdm doctor`

```console
gdm explore --root-id YOUR_ROOT_FOLDER_ID
gdm infer collection --folder-id YOUR_FOLDER_ID --model-name CampaignAsset
gdm --project-dir . adopt --model-name CampaignAsset --folder-id YOUR_FOLDER_ID
gdm --project-dir . doctor --model CampaignAsset --folder-id YOUR_FOLDER_ID
```

Pass `--apply` globally to perform remote registry/metadata writes during `adopt`.

## Example model

<!-- fmt:off -->
```python
--8<-- "models_campaign_asset.py"
```
<!-- fmt:on -->

Use [`DriveObjectMixin`](../api/index.md#quick-reference) to include standard Drive identity fields without boilerplate.

## Native formats

| Format | GDM approach |
| --- | --- |
| Google Docs / Slides | Metadata via model; bodies via `content.google_doc()` / `content.google_slides()` |
| Images / PDFs / video | `content.download()` / `content.upload()` |
| Google Sheets in folder | Native sheet content **or** bind worksheet as `storage="sheet"` table |
| JSON sidecars | Opt-in `codec="json"` on `storage="file"` greenfield collections |

Export (`content.export(...)`) produces portable PDF/Office/text views and **never** replaces native Workspace MIME types in place.

## Routing metadata

GDM stores only small keys in private `appProperties`:

| Key | Purpose |
| --- | --- |
| `gdm_project` | Project UUID |
| `gdm_model` | Model storage name |
| `gdm_schema` | Schema version |

Drive limits: **30 keys per app per file**, **124 UTF-8 bytes** per value. Full records and indexes never live in properties.

## Duplicate names and IDs

Drive permits duplicate filenames. GDM preserves that behavior:

- `find_by_name` returns all matches
- `one_by_name` raises [`AmbiguousResourceError`](../consistency/errors.md) with candidate IDs
- Bindings and writes always use **immutable file IDs**

## Permissions

Exploring and adopting arbitrary existing content requires broader OAuth scopes than `drive.file`. See [Authentication](../auth.md).

## Related

- [Drive collections](../storage/collections.md)
- [Explore your Drive](../exploration/index.md)
- [Model inference](../exploration/inference.md)
