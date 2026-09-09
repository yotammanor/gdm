# Testing

GDM ships with pytest suites using **backend test doubles** so CI does not require Google
credentials.

## Unit and integration tests

```console
uv run pytest --cov=gdm --cov-report=term-missing
```

CI runs the full suite on Python **3.11–3.14** with the coverage gate configured in
`pyproject.toml`.

## Live tests (opt-in)

Tests marked `live` call real Google APIs:

```console
export GDM_LIVE=1
export GDM_TEST_ROOT_FOLDER_ID=your_dedicated_root
export GDM_TEST_TOKEN_PATH=/path/to/token.json
# or: export GDM_TEST_SERVICE_ACCOUNT_FILE=/path/to/sa.json
uv run pytest -m live -v
```

| Variable | Purpose |
| --- | --- |
| `GDM_LIVE=1` | Enable live tests |
| `GDM_TEST_ROOT_FOLDER_ID` | Dedicated Drive root folder |
| `GDM_TEST_TOKEN_PATH` | OAuth user token JSON |
| `GDM_TEST_SERVICE_ACCOUNT_FILE` | Service account JSON |

Never commit tokens or keys.

## Test doubles

```python
from gdm.backends.memory import MemoryBackend
from gdm.testing import memory_session, memory_backend
from gdm.session import Session

with memory_session() as session:
    ...
```

Use doubles to simulate conflicts, partial failures, and quota errors deterministically.

Run docs examples locally without Google (scripts with `GDM_USE_FAKE` support):

```console
GDM_USE_FAKE=1 python docs_src/quickstart_collection.py
GDM_USE_FAKE=1 python docs_src/quickstart_session.py
GDM_USE_FAKE=1 python docs_src/query_basic.py
GDM_USE_FAKE=1 python docs_src/query_sheet.py
GDM_USE_FAKE=1 python docs_src/crud_create.py
```

## Markers

```python
import pytest


@pytest.mark.live
def test_roundtrip(live_client): ...
```

## CI

| Workflow | Purpose |
| --- | --- |
| [`ci.yml`](https://github.com/yotammanor/gdm/blob/main/.github/workflows/ci.yml) | gitleaks, Ruff, format, ty, pytest+coverage, build, strict docs |
| [`pages.yml`](https://github.com/yotammanor/gdm/blob/main/.github/workflows/pages.yml) | Strict MkDocs build and GitHub Pages deploy — **after green CI on `main`** |
| [`live-tests.yml`](https://github.com/yotammanor/gdm/blob/main/.github/workflows/live-tests.yml) | Manual suite — **11** Drive/Docs/Slides/Sheets tests |

The live workflow sets `GDM_LIVE=1`, takes the dedicated root ID as a dispatch
input (`project_root` → `GDM_TEST_ROOT_FOLDER_ID`), and accepts either an
authorized-user token in `GDM_OAUTH_TOKEN` or a service-account key in
`GDM_GCP_KEY`. Credentials are written to ephemeral runner paths only; workflow
logs never echo secret values. The account represented by either secret must be
able to edit the root.

## Related

- [Contributing](https://github.com/yotammanor/gdm/blob/main/CONTRIBUTING.md)
- [Authentication](auth.md)
