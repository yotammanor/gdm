# Documentation source examples

Runnable examples referenced from MkDocs via snippet includes (`--8<-- "file.py"`).

Examples use the public API:

```python
from gdm import GDM, Field, GDMModel, Session, select
from gdm.project import ResourceKind
from gdm.testing import memory_session
```

## Running locally

```console
uv sync --all-groups
uv pip install -e ".[oauth]"
GDM_USE_FAKE=1 uv run python docs_src/quickstart_collection.py
GDM_USE_FAKE=1 uv run python docs_src/quickstart_session.py
GDM_USE_FAKE=1 uv run python docs_src/query_basic.py
GDM_USE_FAKE=1 uv run python docs_src/query_sheet.py
GDM_USE_FAKE=1 uv run python docs_src/crud_create.py
```

Set `GDM_USE_FAKE=1` on the scripts above to exercise examples against
`gdm.testing.memory_session()` without Google credentials.

Collection examples that call `GDM.open(...)` and `client.collection()` in their
live paths require credentials or test doubles wired in your own scripts.

## Files

| File | Used in | `GDM_USE_FAKE` |
| --- | --- | --- |
| `quickstart_collection.py` | `docs/index.md` | Yes (seeded memory backend) |
| `quickstart_session.py` | Sheet quickstart / fake runs | Yes |
| `models_campaign_asset.py` | Collection tutorial, adoption | — |
| `models_task.py` | Sheet tutorial | — |
| `models_inventory.py` | Adoption: existing sheet | — |
| `models_release_note.py` | Collection codecs | — |
| `collection_operations.py` | Collection safety semantics | No (live Drive only) |
| `collection_content.py` | Native content handles | No (live Drive only) |
| `query_basic.py` | Collection metadata queries | Yes |
| `query_sheet.py` | Sheet row queries | Yes |
| `crud_create.py` | Sheet CRUD | Yes |

Import paths in multi-file examples assume `docs_src/` is on `PYTHONPATH` or run via `uv run python docs_src/...` from the repo root with `src` installed editable.
