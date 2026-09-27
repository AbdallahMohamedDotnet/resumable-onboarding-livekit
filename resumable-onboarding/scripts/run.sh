#!/usr/bin/env bash
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
uv_bin="${UV_BIN:-uv}"
case_name="$(basename "$0" .sh)"
if [[ "$case_name" == run ]]; then
  case_name="${1:-help}"
  if [[ $# -gt 0 ]]; then shift; fi
fi
load_env() {
  if [[ ! -f .env.local ]]; then
    printf 'Missing .env.local; copy and edit .env.example first.\n' >&2
    exit 1
  fi
  set -a
  source .env.local
  set +a
}
agent_pid_file="$root/run/agent.pid"
agent_running() {
  [[ -f "$agent_pid_file" ]] || return 1
  local pid
  pid="$(cat "$agent_pid_file")"
  kill -0 -- "-$pid" 2>/dev/null || kill -0 "$pid" 2>/dev/null
}
start_server() {
  load_env
  if docker inspect resumable-onboarding-livekit >/dev/null 2>&1; then
    if [[ "$(docker inspect -f '{{.State.Running}}' resumable-onboarding-livekit)" == true ]]; then return; fi
    docker start resumable-onboarding-livekit >/dev/null
    return
  fi
  [[ "$LIVEKIT_API_KEY" =~ ^[A-Za-z0-9_-]+$ ]] || { echo 'Invalid LiveKit key' >&2; exit 1; }
  [[ "$LIVEKIT_API_SECRET" =~ ^[A-Za-z0-9_-]{32,}$ ]] || { echo 'LiveKit secret must be at least 32 safe characters' >&2; exit 1; }
  [[ "$LIVEKIT_API_SECRET" != replace-with-a-secret-of-at-least-32-characters ]] || { echo 'Replace the example LiveKit secret' >&2; exit 1; }
  mkdir -p run
  chmod 700 run
  umask 077
  cat > run/livekit.yaml <<CONFIG
port: 7880
rtc:
  tcp_port: 7881
  port_range_start: 50000
  port_range_end: 50020
keys:
  $LIVEKIT_API_KEY: $LIVEKIT_API_SECRET
CONFIG
  docker run -d --name resumable-onboarding-livekit --network host \
    -v "$root/run/livekit.yaml:/etc/livekit.yaml:ro" \
    livekit/livekit-server:latest --config /etc/livekit.yaml >/dev/null
  for _ in {1..30}; do
    if curl -fsS http://127.0.0.1:7880/ >/dev/null 2>&1; then return; fi
    sleep 1
  done
  echo 'LiveKit server did not become ready' >&2
  exit 1
}
start_agent() {
  load_env
  if agent_running; then return; fi
  mkdir -p run
  chmod 700 run
  "$uv_bin" run python cli.py --json doctor >/dev/null
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  setsid "$uv_bin" run python agent.py start >run/agent.log 2>&1 &
  echo "$!" > "$agent_pid_file"
  for _ in {1..30}; do
    if curl -fsS http://127.0.0.1:8081/ >/dev/null 2>&1 && docker logs --since "$started" resumable-onboarding-livekit 2>&1 | grep -q 'worker registered'; then return; fi
    if ! agent_running; then cat run/agent.log >&2; exit 1; fi
    sleep 1
  done
  echo 'Agent did not become ready; see run/agent.log' >&2
  exit 1
}
stop_agent() {
  if agent_running; then
    local_pid="$(cat "$agent_pid_file")"
    kill -TERM -- "-$local_pid"
    for _ in {1..30}; do
      if ! kill -0 -- "-$local_pid" 2>/dev/null; then break; fi
      sleep 0.1
    done
  fi
  if [[ -f "$agent_pid_file" ]]; then mv "$agent_pid_file" "$agent_pid_file.stopped"; fi
}
case "$case_name" in
  setup)
    "$uv_bin" sync --locked
    if [[ ! -f .env.local ]]; then cp .env.example .env.local; chmod 600 .env.local; fi
    "$uv_bin" run python cli.py doctor
    ;;
  doctor) "$uv_bin" run python cli.py doctor "$@" ;;
  start) mode="${1:-all}"; if [[ "$mode" == all ]]; then start_server; fi; start_agent ;;
  stop) mode="${1:-all}"; stop_agent; if [[ "$mode" == all ]]; then docker stop resumable-onboarding-livekit >/dev/null 2>&1 || true; fi ;;
  restart) mode="${1:-agent}"; stop_agent; if [[ "$mode" == all ]]; then docker restart resumable-onboarding-livekit >/dev/null; fi; start_agent ;;
  console)
    load_env
    read -r -p 'Onboarding ID: ' ONBOARDING_CONSOLE_ID
    read -r -s -p 'Resume credential: ' ONBOARDING_CONSOLE_CREDENTIAL
    printf '\n'
    export ONBOARDING_CONSOLE_ID ONBOARDING_CONSOLE_CREDENTIAL
    lk agent console agent.py "$@"
    ;;
  take) "$uv_bin" run python take_console.py "$@" ;;
  new) "$uv_bin" run python cli.py new "$@" ;;
  resume) "$uv_bin" run python cli.py resume "$@" ;;
  inspect) "$uv_bin" run python cli.py "$@" ;;
  backup) "$uv_bin" run python cli.py backup "$@" ;;
  test) "$uv_bin" run python -m pytest -q "$@" ;;
  simulate) "$uv_bin" run python -m pytest -q tests/test_recovery.py "$@" ;;
  help|*) echo 'Usage: scripts/run.sh {setup|doctor|start [all|agent]|stop [all|agent]|restart [all|agent]|console|take [--resume SESSION_FILE]|new|resume|inspect|backup|test|simulate}' ;;
esac
