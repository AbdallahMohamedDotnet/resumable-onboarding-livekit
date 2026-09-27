# Project guidance

This is a self-hosted LiveKit Agents project. Use `uv` and the checked-in lockfile.
Keep durable business policy and SQLite transactions in `state.py`, the native
LiveKit runtime in `agent.py`, and operator commands in `cli.py`.
Do not use LiveKit Cloud inference or deployment commands. Refer to current
LiveKit documentation and verify APIs against the pinned package before edits.
Run `uv run ruff check .` and `uv run python -m pytest` before committing.
