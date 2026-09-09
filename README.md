# Google Drive Model

<p align="center">
  <em>Pydantic models and a SQLModel-inspired session for Google Drive folders and Sheets.</em>
</p>

<p align="center">
  <a href="https://github.com/yotammanor/gdm/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/yotammanor/gdm/ci.yml?branch=main&label=CI" alt="CI status"></a>
  <a href="https://github.com/yotammanor/gdm/blob/main/LICENSE"><img src="https://img.shields.io/github/license/yotammanor/gdm.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11–3.14-blue.svg" alt="Python 3.11–3.14"></a>
  <a href="https://pydantic.dev"><img src="https://img.shields.io/badge/Pydantic-v2-3776AB.svg" alt="Pydantic v2"></a>
  <img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Development status: alpha">
</p>

---

**GDM** maps Pydantic models onto **existing Drive folders** and **Google Sheets rows** with a familiar `Session`, `select()`, and `Field()` API—without pretending Drive is a relational database.

Center your app on a **mixed folder** already full of Google Docs, Slides, images, PDFs, shortcuts, and nested subfolders. GDM maps Drive metadata with `Field(drive=...)`, application metadata with `Field(metadata=...)`, and keeps **native bodies lazy** until you call `object.content`.

> **Distribution vs import name:** install **`google-drive-model`**. The PyPI
> name `gdm` is taken by an unrelated dependency manager. Import and CLI use
> **`gdm`**:
>
> ```python
> from gdm import GDM, DriveObject, Field, select
> ```

**Documentation:** [yotammanor.github.io/gdm](https://yotammanor.github.io/gdm/)
· [docs source](https://github.com/yotammanor/gdm/tree/main/docs)
· **Design:** [`docs/design.md`](https://github.com/yotammanor/gdm/blob/main/docs/design.md)

---

## Why GDM?

| You have… | GDM gives you… |
| --- | --- |
| A launch folder with Docs, decks, and PNGs | `DriveCollection[T]` over direct children; MIME filters; lazy native content |
| A team spreadsheet with extra columns | Progressive sheet mapping—bind declared fields, preserve the rest |
| Scripts that need typed reads/writes | Pydantic validation on every boundary crossing |
| Multiple apps on one Google account | Root-scoped projects with isolated `.gdm/` metadata |

GDM is **honest about platform limits**: no cross-file transactions, no server-side joins, no row-level security. Drive permissions remain the security boundary.

---

## Installation

Install from PyPI:

```console
pip install "google-drive-model[oauth]"
```

To work from source:

```console
git clone https://github.com/yotammanor/gdm.git
cd gdm
uv sync --all-groups --frozen
```

| Extra | Purpose |
| --- | --- |
| `oauth` | Interactive installed-app login (`google-auth-oauthlib`) |
| `yaml` | YAML codec for opt-in structured file collections |

---

## Quickstart: adopt a Drive folder

### 1. Authenticate and open a project

```console
gdm auth login
gdm --project-dir . --apply init --root-id YOUR_ROOT_FOLDER_ID --name my-app --remote
```

This creates the shared project manifest, registry, and collection metadata area
under the Drive root. Tokens and local schema/index state stay local.

### 2. Define a collection model

```python
from gdm import DriveObject, Field


class CampaignAsset(DriveObject, collection="Campaigns/Launch"):
    campaign: str | None = Field(default=None, metadata="campaign")
    approved: bool = Field(default=False, metadata="approved")
```

`DriveObject` is `GDMModel` + `storage="collection"` + read-only Drive fields (`drive_id`, `name`, `mime_type`, …). A model with no application metadata at all is just `class Asset(DriveObject, collection="Assets"): pass`.

### 3. Connect, bind, and use the collection

```python
from gdm import GDM, select


client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
client.bind_collection(
    CampaignAsset,
    path="Campaigns/Launch",  # resolved once; immutable folder ID is persisted
)

assets = client.collection(CampaignAsset)
images = assets.where_mime("image/*")
logo = images.get("IMAGE_FILE_ID")
brief = assets.get("GOOGLE_DOC_FILE_ID")

data = logo.content.download()  # explicit — metadata queries skip bodies
doc = brief.content.google_doc()  # native Docs API handle
doc.batch_update([...])

with client.session() as session:
    approved = session.exec(
        select(CampaignAsset).where(CampaignAsset.approved == True)  # noqa: E712
    ).all()
```

Collection operations are **explicit about safety**: `adopt` vs `move` vs `shortcut`, `detach` vs `trash`, and `delete_permanently` for irreversible removal. `session.delete()` trashes collection members; it never silently purges Drive files.

See the [Drive collections tutorial](https://yotammanor.github.io/gdm/tutorial/collection/) for duplicate names, shortcuts, recursion, and metadata reconciliation.

---

## Sheet tables (second story)

Bind a worksheet without rewriting your team's sheet:

```python
from gdm import Field, GDMModel


class Task(GDMModel, storage="sheet", mapping="partial", source="Tasks/Q1"):
    id: str | None = Field(default=None, primary_key=True)
    title: str
    status: str = "todo"
```

```python
from gdm import GDM, ResourceKind, select


client = GDM.open(root_folder_id="YOUR_ROOT_FOLDER_ID")
client.bind(
    Task,
    resource_id="YOUR_SPREADSHEET_ID",
    kind=ResourceKind.SPREADSHEET,
    worksheet="Tasks",
)
with client.session() as session:
    open_tasks = session.exec(select(Task).where(Task.status == "open").limit(50)).all()
    session.add(Task(title="Ship docs", status="open"))
    session.commit()
```

Foreign keys can reference **Drive file IDs** from collection models. Relationships resolve client-side with `session.prefetch()`—see [Relationships](https://yotammanor.github.io/gdm/storage/relationships/).

---

## CLI

| Command | Purpose |
| --- | --- |
| `gdm init` | Local `.gdm/` scaffolding; optional remote project with `--apply --remote` |
| `gdm explore` | Inventory a folder (metadata only—zero content reads) |
| `gdm infer collection` | Suggest a collection model from Drive metadata |
| `gdm infer file` | Infer from a local JSON/text/CSV sample |
| `gdm adopt` | Bind a folder (dry-run by default) |
| `gdm doctor` | Metadata reconciliation, drift, scale warnings |
| `gdm migrate plan/apply/rollback` | Forward, resumable migrations with declared rollback steps |
| `gdm index rebuild` | Rebuild optional sidecar indexes |
| `gdm auth login` | Interactive OAuth |

Terminal demo (**actual local CLI recording**, no live OAuth or Drive mutations):

<p align="center">
  <a href="https://github.com/yotammanor/gdm/blob/main/docs/assets/demo.cast"><img src="https://raw.githubusercontent.com/yotammanor/gdm/main/docs/assets/demo.gif" alt="Recorded GDM CLI demo — open demo.cast to play" width="740"></a>
</p>

Regenerate the canonical cast (and the GIF when `agg` is installed):
`bash docs/assets/regenerate-demo.sh`

---

## Examples

| Example | What it shows |
| --- | --- |
| [`examples/fastapi_assets/`](https://github.com/yotammanor/gdm/tree/main/examples/fastapi_assets) | A JSON API over a Drive folder: session per request, read-only Drive fields, 409 on concurrent edits, explicit content reads |

Examples run offline against seeded data, so you can try one before connecting a
Drive:

```console
uv sync --all-groups
uv run --all-groups uvicorn --app-dir examples fastapi_assets.demo:app --reload
```

Single-purpose documentation snippets live in [`docs_src/`](https://github.com/yotammanor/gdm/tree/main/docs_src).

---

## Architecture at a glance

```text
Your Pydantic models (GDMModel + Field)
        │
        ▼
   GDM client ──► DriveCollection[T]  (folder-native CRUD, lazy content)
        │              │
        │              └──► metadata Sheet under .gdm/collections/
        ▼
   Session / select()  ──► expression tree (metadata queries; no body fetch)
        │
        ├──► Sheets backend (rows, batchUpdate)
        └──► Drive backend (files, appProperties routing keys)
        │
        ▼
   Drive .gdm/ metadata (manifest, registry, collection metadata)
   Local .gdm/ tooling state (schema snapshots, migration checkpoints, indexes)
```

**Key limitations** (by design):

- `Session.commit()` coordinates writes but is **not** an ACID transaction across files.
- Metadata queries never download native bodies; content access is explicit via `.content`.
- Relationships and joins are **client-side** with batched prefetch.
- Export produces portable views; native Docs/Slides mutation uses their respective APIs.
- Docs and Slides use atomic revision preconditions. Drive binary and Sheets
  updates use best-effort conflict checks because those APIs expose no general
  conditional write precondition.

Full rationale: [`docs/design.md`](https://github.com/yotammanor/gdm/blob/main/docs/design.md).

---

## Roadmap

Everything described above is implemented and tested. GDM is still alpha, so pin
`google-drive-model==0.1.*` while the 0.x line moves.

### 0.2 — fewer API calls, and async

- [ ] Push the `select()` predicates Drive can evaluate into `files.list(q=…)`. Today every predicate, and every exact `mime=` selector, is filtered client-side after a bounded scan.
- [ ] Async client and session, so an `async def` route can await GDM instead of running it in a threadpool.
- [ ] Coalesce a multi-record `commit()` into one `batchUpdate` per resource rather than one call per record.
- [ ] Cursor-based pagination for collections and row tables, replacing today's `scan_limit` ceiling.

### 0.3 — staying fresh at scale

- [ ] Feed `client.changes` (Drive Changes API) into index invalidation so sidecar indexes refresh without `gdm index rebuild`.
- [ ] Drive push notifications (watch channels) next to today's polling.
- [ ] Incremental `gdm doctor` that reconciles only what moved.
- [ ] Published benchmarks for 10k-file folders and shared drives.

### Later

- [ ] Declarative relationship attributes over today's explicit `session.prefetch()`.
- [ ] More metadata stores behind the existing protocol, starting with a SQLite sidecar for local-first apps.
- [ ] Migration operations that transform content, not only metadata.
- [ ] Deployment recipes for service accounts and domain-wide delegation.

### Not planned

Platform limits rather than backlog: cross-file ACID transactions, server-side
joins, row-level security, and SQL-style aggregate pushdown. Drive permissions
stay the security boundary.

Missing a use case? [Open an issue](https://github.com/yotammanor/gdm/issues).

---

## Contributing

See [CONTRIBUTING.md](https://github.com/yotammanor/gdm/blob/main/CONTRIBUTING.md), [SECURITY.md](https://github.com/yotammanor/gdm/blob/main/SECURITY.md), and [CODE_OF_CONDUCT.md](https://github.com/yotammanor/gdm/blob/main/CODE_OF_CONDUCT.md).

```console
uv run --all-groups pytest
uv run ruff check .
uv run mkdocs build --strict
```

---

## License

MIT © [Yotam Manor](https://github.com/yotammanor)
