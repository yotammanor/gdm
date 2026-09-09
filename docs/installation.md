# Installation

Install **google-drive-model** from PyPI:

```console
pip install "google-drive-model[oauth]"
```

!!! note "Distribution name"
    Import and CLI use the short name **`gdm`**. The PyPI project `gdm` is unrelated.

## Requirements

- Python **3.11–3.14** (project default **3.12**)
- A Google Cloud project with **Drive**, **Sheets**, **Docs**, and **Slides** APIs enabled
- Network access to Google APIs

## From source

=== "uv"

    ```console
    git clone https://github.com/yotammanor/gdm.git
    cd gdm
    uv sync --extra oauth --frozen
    uv run gdm --help
    ```

=== "pip"

    ```console
    git clone https://github.com/yotammanor/gdm.git
    cd gdm
    python -m venv .venv
    source .venv/bin/activate
    pip install -e ".[oauth]"
    ```

With an activated pip environment, verify the CLI:

```console
gdm --help
```

## Optional extras

| Extra | Packages | Use when |
| --- | --- | --- |
| `oauth` | `google-auth-oauthlib` | Interactive `gdm auth login` |
| `yaml` | `pyyaml` | YAML codecs for file-backed models |

```console
uv pip install -e ".[oauth,yaml]"
```

## Google Cloud setup

1. Create or select a [Google Cloud project](https://console.cloud.google.com/).
2. Enable **Google Drive API**, **Google Sheets API**, **Google Docs API**, and **Google Slides API**.
3. Configure OAuth consent (installed app) or a service account for automation.

Details: [Authentication](auth.md).

## Development install

See [Contributing](https://github.com/yotammanor/gdm/blob/main/CONTRIBUTING.md) for editable installs, pytest, Ruff, and MkDocs.

```console
uv run pytest
uv run mkdocs serve --strict
```

## Next

[Authenticate](auth.md) and [Drive collections tutorial](tutorial/collection.md).
