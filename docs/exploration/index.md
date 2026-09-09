# Explore your Drive

**`gdm explore`** inventories a Drive folder and reports collection candidates **without
reading file bodies**.

```console
gdm explore --root-id YOUR_ROOT_FOLDER_ID
gdm explore --root-id ID --json
```

## Output

Explore reports:

- direct vs nested child counts
- MIME type distribution
- collection folder candidates (including `.gdm` presence)
- duplicate-name and shortcut warnings where detectable
- explicit **zero content reads** guarantee

## Scope

Exploration uses credentials visible to the authenticated principal. Broader Drive scopes
are required for arbitrary existing content—see [Authentication](../auth.md).

## Duplicate paths

Drive permits duplicate filenames. Exploration surfaces ambiguities; bindings always
persist resolved **IDs**, not paths alone.

## Python API

```python
from gdm import GDM

client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
report = client.explore()
print(report.inventory.direct_count, report.inventory.zero_content_reads)
```

Lower-level access:

```python
from gdm.auth import build_explorer_service
from gdm.explore import Explorer

explorer = Explorer(build_explorer_service(use_adc=True))
report = explorer.explore("YOUR_ROOT_FOLDER_ID")
```

## Related

- [Model inference](inference.md)
- [Adopt Drive folders](../adoption/drive-collections.md)
- [CLI](../cli/index.md)
