# FastAPI over a Drive folder

A small JSON API whose database is a Google Drive folder: list and filter the
files in it, edit the application metadata GDM keeps for them, and download a
file body.

| File | Role |
| --- | --- |
| [`models.py`](models.py) | The `CampaignAsset` collection model |
| [`app.py`](app.py) | Dependencies, routes, and the app factory |
| [`demo.py`](demo.py) | The same app on seeded in-memory data |

## Run it without Google credentials

```console
uv sync --all-groups
uv run --all-groups uvicorn --app-dir examples fastapi_assets.demo:app --reload
```

Open <http://127.0.0.1:8000/docs>. Every metadata route works against seeded
data, so you can explore the API before pointing it at a real Drive.

## Run it against Drive

Create the project once (see the [quickstart](https://yotammanor.github.io/gdm/)),
then point the app at it:

```console
export GDM_ROOT_FOLDER_ID=...      # project root passed to `gdm init`
export GDM_ASSETS_FOLDER_ID=...    # optional: bind by folder ID
uv run --all-groups uvicorn --app-dir examples fastapi_assets.app:app
```

Without `GDM_ASSETS_FOLDER_ID`, the app binds using the model's
`collection="Campaigns/Launch"` path hint instead.

## Routes

| Route | Behaviour |
| --- | --- |
| `GET /assets` | List assets, optionally filtered by `campaign` and `approved` |
| `GET /assets/{drive_id}` | One asset, keyed by its Drive file ID |
| `PATCH /assets/{drive_id}` | Update `campaign` / `approved` |
| `GET /assets/{drive_id}/content` | Serve the file body |

## What it demonstrates

**One session per request.** `get_session` yields a `Session` and closes it when
the response is done, the same shape as a SQLModel/FastAPI dependency.

**Drive owns its own fields.** `AssetUpdate` accepts only `campaign` and
`approved`, so a client cannot rename a file or forge `drive_id` through the
API. Anything else in the body is a 422.

**Concurrent edits surface as 409.** If someone edits the metadata sheet in
Drive between the read and the commit, `session.commit()` raises `ConflictError`
and the route returns 409 rather than overwriting their edit.

**Content stays explicit.** Only `/content` reads a body. Listing a thousand
assets never downloads a byte. Native Docs and Slides have no binary body, so
that route falls back to a PDF export.

**GDM models are Pydantic models.** `CampaignAsset` is used directly as the
response type, so it appears in the generated OpenAPI schema with no
intermediate DTO.

**Routes are `def`, not `async def`.** GDM is synchronous, so FastAPI runs these
in its threadpool instead of blocking the event loop on Google API calls. An
async client is on the [roadmap](../../README.md#roadmap).

## Tests

[`tests/test_examples_fastapi.py`](../../tests/test_examples_fastapi.py) runs
this app in CI with no credentials, by overriding the two dependencies with an
in-memory backend:

```console
uv run --all-groups pytest tests/test_examples_fastapi.py
```

That is the pattern to copy for your own app's tests.
