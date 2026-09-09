# Pull request template

## Summary

<!-- What changed and why? -->

## Test plan

- [ ] `uv run pytest`
- [ ] `uv run ruff check .`
- [ ] `uv run ruff check docs_src` (if docs examples changed)
- [ ] `uv run mkdocs build --strict` (if docs changed)

## Docs

- [ ] Updated user docs / `docs_src/` examples (if API behavior changed)
