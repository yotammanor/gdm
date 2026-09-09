# Releasing

GDM releases are built from an annotated `v*` tag by
`.github/workflows/release.yml`. The workflow rebuilds from the lockfile,
checks metadata and installation, publishes to PyPI with OIDC, and attaches
the exact wheel and sdist to a GitHub Release.

## One-time PyPI setup

Configure a pending trusted publisher for the `google-drive-model` project:

- owner: `yotammanor`
- repository: `gdm`
- workflow: `release.yml`
- environment: `pypi`

No long-lived PyPI token is stored in GitHub.

## Release checklist

1. Update `version` in `pyproject.toml` and add dated notes to `CHANGELOG.md`.
2. Regenerate `uv.lock` from public PyPI and verify that it contains no private
   registry URLs.
3. Run Ruff, ty, the complete unit suite with coverage, strict docs build,
   package build, `twine check`, and a clean-wheel import/CLI smoke test.
4. Run the opt-in live Google suite against its dedicated root and confirm that
   cleanup leaves no test resources.
5. Commit and push `main`; wait for CI, docs, and secret scanning to pass.
6. Create and push an annotated tag matching the package version, for example
   `v0.1.0`.
7. Verify the GitHub Release artifacts and install the published wheel from
   PyPI in a clean environment.

The tag/package mismatch check fails closed before publishing.
