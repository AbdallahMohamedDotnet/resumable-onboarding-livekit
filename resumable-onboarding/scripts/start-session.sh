#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
uv_bin="${UV_BIN:-uv}"

if ! command -v lk >/dev/null 2>&1; then
  echo 'LiveKit CLI (lk) is required for the session console.' >&2
  exit 1
fi

"$uv_bin" run python take_console.py "$@"
