# Contributing to GDM

Thank you for your interest in **Google Drive Model** (import name: `gdm`).

## Getting started

1. Fork and clone [yotammanor/gdm](https://github.com/yotammanor/gdm).
2. Install [uv](https://docs.astral.sh/uv/) (recommended) or use another
   PEP 517-compatible installer.
3. Sync the locked development environment (the project is installed editable):

   ```console
   uv sync --all-groups --frozen
   ```

4. Run the test suite:

   ```console
   uv run pytest --cov=gdm --cov-report=term-missing
   ```

## Development workflow

- Create a branch from `main` for your change.
- Keep pull requests focused; unrelated refactors belong in separate PRs.
- Update docs and `docs_src/` examples when you change public API behavior. User docs
  target the public surface (`from gdm import GDM, …`, `client.collection()`, class keywords).
- Add tests for bug fixes and new behavior.

## Code quality

Before opening a PR, run:

```console
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest --cov=gdm --cov-report=term-missing
uv run mkdocs build --strict
```

CI runs the same checks on Python 3.11–3.14 (default **3.12**), including gitleaks,
Ruff on `docs_src/`, and strict MkDocs.

## Documentation

User-facing docs live under `docs/` and are built with MkDocs Material:

```console
uv run mkdocs serve --strict
```

Runnable snippets belong in `docs_src/` and are included with
[`--8<--`](https://facelessuser.github.io/pymdown-extensions/extensions/snippets/)
or copied into pages with clear context.

Architecture decisions are recorded in [`docs/design.md`](docs/design.md).
Update that document only for significant design changes.

## Live Google tests

Tests marked `live` require real Google credentials and a dedicated Drive root.
The manual workflow accepts `GDM_OAUTH_TOKEN` or `GDM_GCP_KEY`; locally use:

```console
export GDM_LIVE=1
export GDM_TEST_ROOT_FOLDER_ID=your_root
export GDM_TEST_TOKEN_PATH=/path/to/token.json
# or: export GDM_TEST_SERVICE_ACCOUNT_FILE=/path/to/key.json
uv run pytest -m live
```

The suite currently includes **11** Drive/Docs/Slides/Sheets integration tests.
Do not commit tokens, refresh tokens, or service-account keys.

## Pull requests

- Use a clear title and describe **why** the change is needed.
- Link related issues when applicable.
- Ensure CI passes (or explain known failures during early development).
- Be respectful; see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Questions

Open a [GitHub issue](https://github.com/yotammanor/gdm/issues) for questions,
bug reports, and ideas.
