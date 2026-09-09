# Contributing

GDM uses [uv](https://docs.astral.sh/uv/) for reproducible development.

```console
git clone https://github.com/yotammanor/gdm.git
cd gdm
uv sync --all-groups --frozen
uv run pytest --cov=gdm --cov-report=term-missing
```

Before opening a pull request, run:

```console
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest --cov=gdm --cov-report=term-missing
uv run mkdocs build --strict
```

Add tests for behavior changes, keep native Drive bodies lazy in metadata-only
flows, and update both user documentation and runnable `docs_src/` examples
when the public API changes.

See the repository
[contribution guide](https://github.com/yotammanor/gdm/blob/main/CONTRIBUTING.md)
for the full workflow and live-test setup.
