# Examples

Runnable applications built on GDM. Each one runs offline against seeded
in-memory data, so you can try it before connecting a Drive, and each is
exercised by the test suite in CI.

| Example | What it shows |
| --- | --- |
| [`fastapi_assets/`](fastapi_assets/) | A JSON API over a Drive folder: session per request, read-only Drive fields, 409 on concurrent edits, explicit content reads |

Short, single-purpose snippets live in [`docs_src/`](../docs_src/) instead;
these are whole apps.

## Setup

```console
uv sync --all-groups
```

Examples need the `examples` dependency group, which `uv run` does not include
by default — pass `--all-groups` when running one.
