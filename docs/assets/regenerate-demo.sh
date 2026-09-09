#!/usr/bin/env bash
# Build an asciinema v2 cast file from actual deterministic no-network GDM CLI stdout.
#
# This does not invoke the interactive `asciinema rec` binary—it runs real `gdm`
# commands locally and writes the v2 event stream in Python.
#
# Usage (from repository root):
#   bash docs/assets/regenerate-demo.sh
#
# Requirements: uv sync --all-groups; no Google credentials or network needed.

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${ROOT}/docs/assets/demo.cast"
SAMPLE_JSON="${ROOT}/docs/assets/demo-sample.json"
TMPDIR="$(mktemp -d)"
TRANSCRIPT="${TMPDIR}/transcript.txt"
trap 'rm -rf "${TMPDIR}"' EXIT

cd "${ROOT}"

cat > "${SAMPLE_JSON}" <<'JSON'
{"title": "Launch brief", "campaign": "Q1", "approved": true}
JSON

if [[ -x "${ROOT}/.venv/bin/gdm" ]]; then
  GDM="${ROOT}/.venv/bin/gdm"
else
  GDM="uv run gdm"
fi

filter_output() {
  sed -E '/FutureWarning/d;/google\.api_core/d;/warnings\.warn/d'
}

append_block() {
  local title="$1"
  shift
  {
    echo "=== ${title} ==="
    "$@" 2>&1 | filter_output || true
    echo
  } >> "${TRANSCRIPT}"
}

: > "${TRANSCRIPT}"

append_block "gdm --help" ${GDM} --help
append_block "gdm infer file (local JSON)" \
  ${GDM} infer file "${SAMPLE_JSON}" --model-name SampleDoc
append_block "gdm init (local metadata only)" \
  ${GDM} --project-dir "${TMPDIR}" init --root-id demo-root-folder --name demo --force
append_block "gdm doctor (local)" \
  ${GDM} --project-dir "${TMPDIR}" doctor
append_block "gdm adopt (dry-run)" \
  ${GDM} --project-dir "${TMPDIR}" adopt --model-name CampaignAsset --folder-id folder-demo-123
append_block "gdm index rebuild (dry-run)" \
  ${GDM} --project-dir "${TMPDIR}" index rebuild \
    --model CampaignAsset --field campaign --resource-id folder-demo-123

python3 - <<'PY' "${TRANSCRIPT}" "${OUT}"
import json
import sys
import time
from pathlib import Path

transcript = Path(sys.argv[1]).read_text(encoding="utf-8")
out = Path(sys.argv[2])

header = {
    "version": 2,
    "width": 100,
    "height": 32,
    "timestamp": int(time.time()),
    "title": "GDM CLI — actual local recording (no network)",
    "env": {"SHELL": "/bin/zsh", "TERM": "xterm-256color"},
}

events: list[list] = []
t = 0.0
section = None

for raw_line in transcript.splitlines():
    if raw_line.startswith("=== ") and raw_line.endswith(" ==="):
        section = raw_line.strip("= ").strip()
        events.append([t, "o", f"\r\n# {section}\r\n"])
        t += 0.2
        continue
    if raw_line.startswith("Usage:") and section == "gdm --help":
        events.append([t, "o", f"$ uv run gdm --help\r\n"])
        t += 0.3
    if raw_line == "":
        events.append([t, "o", "\r\n"])
        t += 0.05
        continue
    events.append([t, "o", raw_line.replace("\n", "\r\n") + "\r\n"])
    t += 0.06

with out.open("w", encoding="utf-8") as fh:
    fh.write(json.dumps(header) + "\n")
    for ev in events:
        fh.write(json.dumps(ev) + "\n")

print(f"Wrote {out}")
PY

if command -v agg >/dev/null 2>&1; then
  agg --renderer fontdue \
    --cols 100 \
    --rows 32 \
    --font-size 12 \
    --fps-cap 5 \
    --speed 3 \
    --idle-time-limit 0.5 \
    --last-frame-duration 1 \
    --theme github-dark \
    "${OUT}" "${ROOT}/docs/assets/demo.gif"
fi

echo "Wrote ${OUT} ($(wc -l < "${OUT}" | tr -d ' ') lines)"
