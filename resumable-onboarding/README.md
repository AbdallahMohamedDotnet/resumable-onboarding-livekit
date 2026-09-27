# Resumable onboarding on self-hosted LiveKit

A single English voice agent collects customer, company, problem, urgency, and follow-up details. LiveKit runs locally for rooms, jobs, audio transport, turn handling, and tool execution. SQLite on one local host is the source of truth for accepted facts, turns, bookings, and summaries. ElevenLabs STT/TTS and OpenRouter LLM remain external services. No LiveKit Cloud account is needed.

## Setup

Install Docker, the [LiveKit CLI](https://docs.livekit.io/reference/developer-tools/livekit-cli/), and [uv](https://docs.astral.sh/uv/). The checked-in Python version is 3.14.7; its linked SQLite version here is 3.53.1. SQLite 3.51.3 or a verified patched backport is needed for the WAL reset fix. Do not use a network filesystem for the database.

```bash
cd resumable-onboarding
./scripts/setup.sh
# Edit .env.local. Set a strong LIVEKIT_API_SECRET, an OpenRouter model that
# supports tool calls, OPENROUTER_API_KEY, ELEVEN_API_KEY, and a voice ID.
./scripts/doctor.sh
./scripts/start.sh all
```

`setup.sh` creates `.env.local` from `.env.example` if absent and never resets the database. The local LiveKit server runs as the project-owned `resumable-onboarding-livekit` Docker container with host networking. Its config and agent PID stay in ignored `run/`. The agent starts through the native LiveKit Agents runtime. The runner checks server and worker readiness. `./scripts/stop.sh all` and `./scripts/restart.sh all` affect only that container and the tracked agent process group. `./scripts/restart.sh agent` leaves the server and database running.

For a production host, configure a reachable TLS WebSocket address, TURN, firewall, and certificates using the [self-hosted deployment guide](https://docs.livekit.io/transport/self-hosting/deployment/). The `ws://localhost:7880` default is for local testing. A second device cannot use its own `localhost` to reach this host. Use a compatible LiveKit client with a secure reachable server address. Never put the server API secret on the client.

## New, resume, and inspect

```bash
./scripts/new.sh
./scripts/inspect.sh sessions
./scripts/inspect.sh state ONBOARDING_ID
./scripts/inspect.sh transcript ONBOARDING_ID --role user
./scripts/inspect.sh operations ONBOARDING_ID
./scripts/inspect.sh followups ONBOARDING_ID
./scripts/inspect.sh summary ONBOARDING_ID
./scripts/inspect.sh export ONBOARDING_ID
./scripts/resume.sh ONBOARDING_ID
./scripts/backup.sh backups/onboarding.sqlite3
./scripts/inspect.sh rotate-resume-credential ONBOARDING_ID
./scripts/inspect.sh reconcile
```

`new` prints an opaque resume credential once, plus a 15-minute room-scoped client token. Store the credential securely. `resume` asks for it without a shell argument, validates its hash, creates a fresh room and participant, advances a database generation, and issues a new token. The newest authorized device wins. The operator-only rotation command recovers from interrupted credential delivery. Names or contact details do not authorize a resume.

`state`, `transcript`, `operations`, `followups`, `summary`, and `export` are separate views. Add `--json` before the subcommand for plain JSON output. `transcript --follow` tails observed events. The summary is deterministic from canonical state and the actual booking, with its source revision and unresolved fields. Historical summaries remain in SQLite. Corrections preserve earlier transcript turns and record old/new values in an operation result. An availability correction invalidates a pending proposal; after a booking it marks rescheduling required and retains the old booking until a replacement is approved.

For a trusted local voice/text console, first create an onboarding, then run `./scripts/console.sh --text` or `./scripts/console.sh`. The shortcut prompts for ID and credential. Console mode simulates a room and does not establish real RTC or cross-device behavior. For real rooms, use the token from `new` or `resume` in an existing compatible LiveKit client.

## Durability and recovery

A finalized turn is written and committed before the LLM can interpret it. A stable source ID links the transcript to its business operation. A transaction applies the accepted answer batch or booking once; the same key and payload returns the stored result, while a changed payload conflicts. Each connection has a generation and a leased executor claim. Stale jobs cannot write new facts. On a fresh job, pending durable turns are replayed in database order through a new native `AgentSession`; a Python session object is never deserialized. `reconcile` checks active attempts and dispatches against the self-hosted server.

The state layer uses WAL, `synchronous=FULL`, foreign keys, a bounded busy timeout, short `BEGIN IMMEDIATE` transactions, a non-destructive schema migration, and SQLite's backup API. The default database is `data/onboarding.sqlite3`, resolved from this project root. The database, WAL files, env files, backups, logs, and exports are ignored by Git. SQLite does not encrypt customer records. Restrict host access and back up separately.

Speech not saved before a crash cannot be reconstructed. An unfinished utterance may need targeted clarification. Recovery cannot resume TTS at an exact audio sample, and a completed speech API call does not prove a human heard it. Process restart recovery does not cover disk loss or a device that violates durability guarantees. Already-buffered audio from an obsolete job cannot be recalled.

## Tests and failure checks

```bash
./scripts/test.sh
./scripts/simulate.sh
./scripts/inspect.sh state ONBOARDING_ID
./scripts/restart.sh agent
./scripts/inspect.sh reconcile
./scripts/restart.sh all
```

`simulate.sh` executes five real subprocess `SIGKILL` checkpoints on an isolated temporary database: after input commit, inside a business transaction, after answer commit, after booking commit before confirmation, and after summary commit. It reopens the same file and checks the transcript, canonical revision, pending work, operations, next action, booking and summary idempotency, and `PRAGMA integrity_check`.

With the local server running, `RUN_LIVEKIT_ROOM_TEST=1 ./scripts/simulate.sh` additionally launches a real agent worker against a temporary database, dispatches a job, waits for its recorded job PID, kills that job with `SIGKILL`, reconciles the stale dispatch, and verifies both immediate job reclaim and a new authorized room takeover. The old executor is fenced. The self-hosted server can briefly continue to report a killed job as running, so reconciliation also checks the local owning PID. This test exercises the room and job path up to participant arrival, without provider inference or conversation replay. To inspect device takeover manually, call `resume` again and compare connection generations and rooms. Duplicate dispatch can be checked by repeating `reconcile` and inspecting `connections` and the room's dispatch list. Corrections, duplicate operations, booking competition, and workflow v1/v2 compatibility are covered by offline tests to the extent noted in the test names.

Real-room client reconnection, provider failures, and cross-device media require a running server, a client, and provider credentials. They are not covered by the ordinary offline suite. Live OpenRouter/ElevenLabs calls incur provider charges and are never run implicitly by setup or tests.

## Design and data flow

- `state.py`: pinned workflow definitions, validation, deterministic next action, SQLite migrations, connections and fencing, idempotent operations, calendar bookings, summaries, and backup.
- `agent.py`: native `AgentServer`/`@rtc_session`, ElevenLabs STT/TTS, OpenRouter LLM, Silero VAD, durable `on_user_turn_completed` barrier, native function tools, controlled spoken questions, and replay in a new session.
- `cli.py`: trusted operator commands, masked resume input, short-lived LiveKit tokens, room/dispatch reconciliation, Rich inspection, JSON export, and doctor.

Customer audio goes to ElevenLabs STT. Relevant conversation and canonical facts go to the configured OpenRouter model. Response text goes to ElevenLabs TTS. Canonical state and transcript stay in local SQLite. No audio is recorded by default. The calendar books one internal resource only; no invitation or email is sent. Appointment duration defaults to 30 minutes; working hours and organization timezone are configurable in `.env.local`.

The OpenRouter model ID is deliberately unset. Choose and verify a model that supports function tools before live use. The first implementation disables preemptive generation and keeps native interruptions. The database contains no automatic deletion policy.
