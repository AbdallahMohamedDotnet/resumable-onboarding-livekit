#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
uv_bin="${UV_BIN:-uv}"
if ! command -v "$uv_bin" >/dev/null 2>&1; then
  if [[ "$uv_bin" == uv && -x "$HOME/.local/bin/uv" ]]; then
    uv_bin="$HOME/.local/bin/uv"
  else
    echo 'uv is required. Install it in ~/.local/bin or set UV_BIN.' >&2
    exit 1
  fi
fi
export UV_BIN="$uv_bin"

if ! command -v lk >/dev/null 2>&1; then
  echo 'LiveKit CLI (lk) is required for the session console.' >&2
  exit 1
fi

"$uv_bin" run python take_console.py "$@"
