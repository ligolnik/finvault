#!/usr/bin/env bash
# Run the same gates CI runs: ruff lint, ruff format, pyright, shellcheck, tests.
# Needs the tools from requirements-dev.txt on PATH (pip install -r requirements-dev.txt).
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

for tool in ruff pyright shellcheck; do
  if ! command -v "$tool" >/dev/null; then
    echo "error: $tool not found — run: pip install -r requirements-dev.txt" >&2
    exit 1
  fi
done

ruff check src tests
ruff format --check src tests
pyright
shellcheck scripts/*.sh
PYTHONPATH=src python3 -m unittest discover -s tests -t . -v
