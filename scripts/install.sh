#!/usr/bin/env bash
# Build finvault as a single-file zipapp and install it at the given path.
#
# Usage: scripts/install.sh <target-path> [python-interpreter]
#   target-path         where to put the executable, e.g. ~/bin/finvault
#   python-interpreter  Python 3.11+ to run it with (default: python3 on PATH)
#
# The interpreter's absolute path becomes the zipapp's shebang, so finvault
# keeps working from shells whose PATH differs. Re-running replaces the file.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 <target-path> [python-interpreter]" >&2
  exit 2
fi
target="$1"
python_cmd="${2:-python3}"

if ! python_path="$(command -v "$python_cmd")"; then
  echo "error: $python_cmd not found — pass a Python 3.11+ interpreter as the second argument" >&2
  exit 1
fi
if ! "$python_path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "error: $python_path is older than 3.11 — pass a newer interpreter as the second argument" >&2
  exit 1
fi

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="$(dirname "$target")"
if [[ ! -d "$target_dir" ]]; then
  echo "error: $target_dir does not exist — create it first (mkdir -p $target_dir)" >&2
  exit 1
fi

stage="$(mktemp -d)"
cleanup() {
  rm -rf "$stage"
  return 0
}
trap cleanup EXIT

mkdir "$stage/app"
rsync -a --exclude '__pycache__' "$repo/src/finvault" "$stage/app/"
cat > "$stage/app/__main__.py" <<'PY'
import sys

from finvault.cli import main

sys.exit(main())
PY

tmp_out="$(mktemp "$target_dir/.finvault.XXXXXX")"
"$python_path" -m zipapp "$stage/app" -p "$python_path" -o "$tmp_out"
chmod 755 "$tmp_out"
mv -f "$tmp_out" "$target"
echo "installed $("$target" --version) at $target (python: $python_path)" >&2
