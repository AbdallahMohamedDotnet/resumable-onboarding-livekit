# Resumable onboarding on self-hosted LiveKit

A single English voice agent collects customer, company, problem, urgency, and follow-up details. LiveKit runs locally for rooms, jobs, audio transport, turn handling, and tool execution. SQLite on one local host is the source of truth for accepted facts, turns, bookings, and summaries. ElevenLabs STT/TTS and OpenRouter LLM remain external services. No LiveKit Cloud account is needed.

## Setup

Install Docker, the [LiveKit CLI](https://docs.livekit.io/reference/developer-tools/livekit-cli/), and [uv](https://docs.astral.sh/uv/). The checked-in Python version is 3.14.7; its linked SQLite version here is 3.53.1. SQLite 3.51.3 or a verified patched backport is needed for the WAL reset fix. Do not use a network filesystem for the database.

```bash
cd resumable-onboarding
uv sync --locked
test -f .env.local || cp .env.example .env.local
# Edit .env.local. Set a strong LIVEKIT_API_SECRET, an OpenRouter model that
# supports tool calls, OPENROUTER_API_KEY, ELEVEN_API_KEY, and a voice ID.
uv run python cli.py doctor
```

If `uv` is installed at `~/.local/bin/uv` but your shell cannot find it, run
`export PATH="$HOME/.local/bin:$PATH"` before the setup commands. Both scripts
also find `~/.local/bin/uv` directly when it is absent from `PATH`.

## Run a local session

Run these commands from `resumable-onboarding` after setup. Each new session prints its onboarding ID and a private resume file path under `run/`. Press Ctrl+C to leave. The local console simulates a room, so neither mode needs a LiveKit server.

**Microphone and speakers:**

```bash
./scripts/start-session.sh --voice
# Continue the same session later (use the printed resume file path):
./scripts/start-session.sh --voice --resume run/taker-ONBOARDING_ID.json
```

Speak as the customer and listen to the agent's replies. Once onboarding and any follow-up booking are finished, the agent asks whether to review the saved information. Say yes to hear each saved answer and confirm or reject it. If an answer is wrong, describe the topic and replacement; the correction is saved before the next answer is read. You can decline the review, and a disconnected review resumes at the same question. Voice mode needs `OPENROUTER_API_KEY`, a tool-capable `OPENROUTER_MODEL`, `ELEVEN_API_KEY`, and `ELEVENLABS_VOICE_ID` in `.env.local`. It uses ElevenLabs STT/TTS and Silero voice activity detection. OpenRouter and ElevenLabs calls can incur charges. If the wrong microphone or speaker is selected, list devices and pass an index or name substring:

```bash
./scripts/start-session.sh --list-devices
./scripts/start-session.sh --voice --input-device 17 --output-device 14
```

The device numbers above are examples; use the numbers from your own list. Headphones help prevent speaker feedback.

**Text only (terminal transcript, no microphone or speakers):**

```bash
./scripts/start-session.sh
# Continue the same session later (use the printed resume file path):
./scripts/start-session.sh --resume run/taker-ONBOARDING_ID.json
```

Type your replies in the terminal. This mode still uses the configured OpenRouter model and saves the conversation to SQLite, but does not use ElevenLabs or audio devices. To inspect a saved transcript separately, run `uv run python cli.py transcript ONBOARDING_ID` or add `--follow` to watch new turns.

For real RTC rooms, start a self-hosted LiveKit server and run `uv run python agent.py start` in another terminal. The existing project-owned `resumable-onboarding-livekit` Docker container can be started with `docker start resumable-onboarding-livekit`. Runtime state stays in ignored `run/` and `data/`.

On Linux, some prebuilt `lk` binaries contain an ALSA data path from their build machine and report `no default input device`. The session script prefers a locally built `$HOME/go/bin/lk` when present; set `ONBOARDING_LK_BIN` to choose another binary. To build LiveKit CLI 2.18.7 against your system audio library, install `portaudio19-dev` and `libasound2-dev`, then run `go install -tags portaudio_system github.com/livekit/livekit-cli/v2/cmd/lk@v2.18.7`. Confirm the fix with `./scripts/start-session.sh --list-devices` before starting a voice session.

For a production host, configure a reachable TLS WebSocket address, TURN, firewall, and certificates using the [self-hosted deployment guide](https://docs.livekit.io/transport/self-hosting/deployment/). The `ws://localhost:7880` default is for local testing. A second device cannot use its own `localhost` to reach this host. Use a compatible LiveKit client with a secure reachable server address. Never put the server API secret on the client.

## New, resume, and inspect

```bash
uv run python cli.py new
uv run python cli.py sessions
uv run python cli.py state ONBOARDING_ID
uv run python cli.py transcript ONBOARDING_ID --role user
uv run python cli.py operations ONBOARDING_ID
uv run python cli.py followups ONBOARDING_ID
uv run python cli.py summary ONBOARDING_ID
uv run python cli.py export ONBOARDING_ID
uv run python cli.py resume ONBOARDING_ID
uv run python cli.py backup backups/onboarding.sqlite3
uv run python cli.py rotate-resume-credential ONBOARDING_ID
uv run python cli.py reconcile
```

`new` prints an opaque resume credential once, plus a 15-minute room-scoped client token. Store the credential securely. `resume` asks for it without a shell argument, validates its hash, creates a fresh room and participant, advances a database generation, and issues a new token. The newest authorized device wins. The operator-only rotation command recovers from interrupted credential delivery. Names or contact details do not authorize a resume.

`state`, `transcript`, `operations`, `followups`, `summary`, and `export` are separate views. Add `--json` before the subcommand for plain JSON output. `transcript --follow` tails observed events. The summary is deterministic from canonical state and the actual booking, with its source revision and unresolved fields. Historical summaries remain in SQLite. Corrections preserve earlier transcript turns and record old/new values in an operation result. Review decisions and the current review position are also saved in SQLite. An availability correction invalidates a pending proposal; after a booking it marks rescheduling required and retains the old booking until a replacement is approved.

The local console does not prove browser or device reconnection. For real rooms, use the token from `new` or `resume` in an existing compatible LiveKit client.

For browser clients, report the browser's IANA time zone through a LiveKit participant attribute after joining the room:

```js
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
if (timezone) {
  await room.localParticipant.setAttributes({ "customer.timezone": timezone });
}
```

The agent reads this attribute when the customer joins and if it changes later. A valid zone is saved before the time zone question, so the question is skipped. If the client does not send one, or sends an invalid zone, the agent asks the customer as before. A saved answer is never replaced by a later device's zone. LiveKit transports participant attributes; it does not determine the customer's time zone itself. The local console has no browser attribute and still asks.

## Durability and recovery

A finalized turn is written and committed before the LLM can interpret it. A stable source ID links the transcript to its business operation. A transaction applies the accepted answer batch or booking once; the same key and payload returns the stored result, while a changed payload conflicts. Each connection has a generation and a leased executor claim. Stale jobs cannot write new facts. On a fresh job, pending durable turns are replayed in database order through a new native `AgentSession`; a Python session object is never deserialized. `reconcile` checks active attempts and dispatches against the self-hosted server.

The state layer uses WAL, `synchronous=FULL`, foreign keys, a bounded busy timeout, short `BEGIN IMMEDIATE` transactions, a non-destructive schema migration, and SQLite's backup API. The default database is `data/onboarding.sqlite3`, resolved from this project root. The database, WAL files, env files, backups, logs, and exports are ignored by Git. SQLite does not encrypt customer records. Restrict host access and back up separately.

Speech not saved before a crash cannot be reconstructed. An unfinished utterance may need targeted clarification. Recovery cannot resume TTS at an exact audio sample, and a completed speech API call does not prove a human heard it. Process restart recovery does not cover disk loss or a device that violates durability guarantees. Already-buffered audio from an obsolete job cannot be recalled.

## Tests and failure checks

```bash
uv run python -m pytest -q
./scripts/simulate-process-kill.sh
./scripts/simulate-process-kill.sh --live  # needs the local LiveKit server
uv run python cli.py state ONBOARDING_ID
uv run python cli.py reconcile
```

`simulate-process-kill.sh` executes five real subprocess `SIGKILL` checkpoints on an isolated temporary database: after input commit, inside a business transaction, after answer commit, after booking commit before confirmation, and after summary commit. It reopens the same file and checks the transcript, canonical revision, pending work, operations, next action, booking and summary idempotency, and `PRAGMA integrity_check`.

With the local server running, `./scripts/simulate-process-kill.sh --live` additionally launches a real agent worker against a temporary database, dispatches a job, waits for its recorded job PID, kills that job with `SIGKILL`, reconciles the stale dispatch, and verifies both immediate job reclaim and a new authorized room takeover. It temporarily stops and restores the project-owned background agent if one is running. The old executor is fenced. The self-hosted server can briefly continue to report a killed job as running, so reconciliation also checks the local owning PID. This test exercises the room and job path up to participant arrival, without provider inference or conversation replay. To inspect device takeover manually, call `resume` again and compare connection generations and rooms. Duplicate dispatch can be checked by repeating `reconcile` and inspecting `connections` and the room's dispatch list. Corrections, duplicate operations, booking competition, and workflow v1/v2 compatibility are covered by offline tests to the extent noted in the test names.

Real-room client reconnection, provider failures, and cross-device media require a running server, a client, and provider credentials. They are not covered by the ordinary offline suite. Live OpenRouter/ElevenLabs calls incur provider charges and are never run implicitly by setup or tests.

For manual failure checks with a configured client and providers:

- Temporary disconnect: join with the issued room token, briefly drop the client network, restore it, then inspect `state` and `transcript`. Native LiveKit reconnect should keep the same onboarding.
- Fresh room or another device: run `resume` with the same ID and credential, join the newly issued room from the other device, and compare `connections`. The server address must be reachable from that device.
- Agent restart or whole-stack restart: restart the background `agent.py start` process or the project-owned Docker server, then run `uv run python cli.py reconcile` and inspect `operations` and `state`. The SQLite file is preserved.
- Duplicate dispatch: run `reconcile` twice and inspect dispatch IDs in `connections`; identical metadata should reuse a live dispatch.
- Delayed stale write: `uv run python -m pytest -q tests/test_state.py -k takeover` exercises the database fence. The opt-in real-room crash test also checks it after a killed job.
- Correction after resume or completion: say “Actually, Thursday works” on the resumed device, then compare `transcript`, `history`, `followups`, and `summary`.
- Interrupted booking confirmation: `./scripts/simulate-process-kill.sh -k after_booking` kills after commit and checks that retry returns one booking.
- Workflow upgrade: `uv run python -m pytest -q -k workflow_version` checks that a v1 record remains pinned when v2 exists.
- Database busy: `uv run python -m pytest -q -k busy_write` holds a real SQLite write lock through the configured five-second timeout and checks that the captured input stays pending without a state revision.

Model failure, TTS failure, and a killed job during a provider-backed reply still require controlled live fault runs. The offline suite proves durable input and committed-state behavior at the documented SQLite checkpoints; it does not substitute for those provider runs.

## Design and data flow

- `state.py`: pinned workflow definitions, validation, deterministic next action, SQLite migrations, connections and fencing, idempotent operations, calendar bookings, summaries, and backup.
- `agent.py`: native `AgentServer`/`@rtc_session`, ElevenLabs STT/TTS, OpenRouter LLM, Silero VAD, durable `on_user_turn_completed` barrier, native function tools, controlled spoken questions, and replay in a new session.
- `cli.py`: trusted operator commands, masked resume input, short-lived LiveKit tokens, room/dispatch reconciliation, Rich inspection, JSON export, and doctor.

Customer audio goes to ElevenLabs STT. Relevant conversation and canonical facts go to the configured OpenRouter model. Response text goes to ElevenLabs TTS. Canonical state and transcript stay in local SQLite. No audio is recorded by default. The calendar books one internal resource only; no invitation or email is sent. Appointment duration defaults to 30 minutes; working hours and organization timezone are configurable in `.env.local`.

The OpenRouter model ID is deliberately unset. Choose and verify a model that supports function tools before live use. The first implementation disables preemptive generation and keeps native interruptions. The database contains no automatic deletion policy.
