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
agent_pid_file="$root/run/agent.pid"
live=0
if [[ "${1:-}" == --live ]]; then
  live=1
  shift
fi

agent_was_running=0
restore_agent() {
  local status=$?
  trap - EXIT
  if (( agent_was_running )); then
    setsid "$uv_bin" run python agent.py start >run/agent.log 2>&1 &
    echo "$!" > "$agent_pid_file"
    local ready=0
    for _ in {1..30}; do
      if curl -fsS http://127.0.0.1:8081/ >/dev/null 2>&1 && \
        grep -q 'registered worker' run/agent.log; then
        ready=1
        break
      fi
      sleep 1
    done
    if (( ! ready )); then
      echo 'Agent did not restart; inspect run/agent.log.' >&2
      status=1
    fi
  fi
  exit "$status"
}

if (( live )); then
  if ! curl -fsS http://127.0.0.1:7880/ >/dev/null 2>&1; then
    echo 'Start the local LiveKit server before using --live.' >&2
    exit 1
  fi
  if [[ -f "$agent_pid_file" ]]; then
    agent_pid="$(cat "$agent_pid_file")"
    if kill -0 -- "-$agent_pid" 2>/dev/null || kill -0 "$agent_pid" 2>/dev/null; then
      agent_was_running=1
      trap restore_agent EXIT
      kill -TERM -- "-$agent_pid"
      for _ in {1..50}; do
        if ! curl -fsS http://127.0.0.1:8081/ >/dev/null 2>&1; then
          break
        fi
        sleep 0.1
      done
      if curl -fsS http://127.0.0.1:8081/ >/dev/null 2>&1; then
        echo 'Agent port 8081 did not become free.' >&2
        exit 1
      fi
    fi
  fi
  RUN_LIVEKIT_ROOM_TEST=1 "$uv_bin" run python -m pytest -q tests/test_recovery.py "$@"
else
  "$uv_bin" run python -m pytest -q tests/test_recovery.py "$@"
fi
