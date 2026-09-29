"""Real SIGKILL checkpoints against a temporary SQLite database."""

import json
import os
import select
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from state import Store


def worker(stage: str, path: str, onboarding_id: str, connection_id: str) -> None:
    store = Store(path)
    if stage == "after_input":
        store.capture(
            onboarding_id,
            connection_id,
            "executor",
            "turn",
            "I am Ahmed",
            {"question": "customer.name"},
        )
    elif stage == "inside_transaction":
        store.capture(
            onboarding_id,
            connection_id,
            "executor",
            "turn",
            "I am Ahmed",
            {"question": "customer.name"},
        )
        with store.write() as db:
            db.execute(
                "UPDATE onboardings SET revision=42 WHERE id=?", (onboarding_id,)
            )
            print("CHECKPOINT", flush=True)
            signal.pause()
    elif stage == "after_state":
        store.capture(
            onboarding_id,
            connection_id,
            "executor",
            "turn",
            "I am Ahmed",
            {"question": "customer.name"},
        )
        store.apply_answers(
            onboarding_id,
            connection_id,
            "executor",
            "turn",
            {"customer.name": {"value": "Ahmed"}},
            0,
        )
    elif stage == "after_booking":
        proposal = store.rows("followups", onboarding_id)[0]
        store.confirm(
            onboarding_id,
            connection_id,
            "executor",
            "approval",
            proposal["id"],
            proposal["proposal_revision"],
            True,
        )
    elif stage == "after_summary":
        store.summary(onboarding_id)
    print("CHECKPOINT", flush=True)
    signal.pause()


@pytest.mark.parametrize(
    "stage",
    [
        "after_input",
        "inside_transaction",
        "after_state",
        "after_booking",
        "after_summary",
    ],
)
def test_sigkill_recovery(tmp_path, stage):
    path = tmp_path / "crash.sqlite3"
    store = Store(path)
    store.migrate()
    onboarding_id, credential = store.create()
    connection_id, _ = store.connect_attempt(
        onboarding_id, credential, "room", "person"
    )
    store.claim(connection_id, "executor")
    if stage == "after_booking":
        start = (
            (datetime.now(UTC) + timedelta(days=8 - datetime.now(UTC).weekday()))
            .replace(hour=12, minute=0, second=0, microsecond=0)
            .isoformat()
            .replace("+00:00", "")
        )
        store.capture(
            onboarding_id,
            connection_id,
            "executor",
            "proposal",
            "Monday at noon",
            {"action": {"kind": "proposal"}},
        )
        proposal = store.propose(
            onboarding_id, connection_id, "executor", "proposal", start, "UTC"
        )
        prompt = f"May I book your follow-up for {proposal['start_utc']} UTC?"
        store.observe_assistant(onboarding_id, connection_id, "ask", prompt, False)
        store.capture(
            onboarding_id,
            connection_id,
            "executor",
            "approval",
            "yes",
            {
                "action": {
                    "kind": "approval",
                    "id": proposal["id"],
                    "revision": proposal["revision"],
                    "text": prompt,
                }
            },
        )
    child = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "worker",
            stage,
            str(path),
            onboarding_id,
            connection_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 10)
        assert ready, "Worker did not reach checkpoint"
        assert child.stdout.readline().strip() == "CHECKPOINT"
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    reopened = Store(path)
    reopened.migrate()
    assert reopened.integrity() == "ok"
    assert len(reopened.rows("transcript_events", onboarding_id)) == (
        3 if stage == "after_booking" else 0 if stage == "after_summary" else 1
    )
    if stage == "after_state":
        assert reopened.get(onboarding_id)["state"]["customer.name"]["value"] == "Ahmed"
        assert reopened.pending_inputs(onboarding_id) == []
        assert reopened.next_action(onboarding_id)["id"] == "customer.contact"
        assert len(reopened.rows("operations", onboarding_id)) == 1
    elif stage == "after_booking":
        assert reopened.rows("followups", onboarding_id)[0]["status"] == "booked"
        assert len(reopened.rows("operations", onboarding_id)) == 2
        assert reopened.pending_inputs(onboarding_id) == []
        assert (
            reopened.confirm(
                onboarding_id,
                connection_id,
                "executor",
                "approval",
                proposal["id"],
                proposal["revision"],
                True,
            )["status"]
            == "booked"
        )
        assert len(reopened.rows("followups", onboarding_id)) == 1
    elif stage == "after_summary":
        saved = reopened.rows("summaries", onboarding_id)
        assert len(saved) == 1
        assert (
            reopened.summary(onboarding_id)["generated_at"]
            == json.loads(saved[0]["summary_json"])["generated_at"]
        )
        assert len(reopened.rows("summaries", onboarding_id)) == 1
    else:
        assert reopened.get(onboarding_id)["revision"] == 0
        assert len(reopened.pending_inputs(onboarding_id)) == 1
        assert reopened.next_action(onboarding_id)["id"] == "customer.name"
        assert reopened.rows("operations", onboarding_id) == []


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "worker":
    worker(*sys.argv[2:])


@pytest.mark.live
@pytest.mark.skipif(
    os.getenv("RUN_LIVEKIT_ROOM_TEST") != "1",
    reason="Requires self-hosted LiveKit server and explicit opt-in",
)
def test_real_job_sigkill_and_takeover(tmp_path, monkeypatch):
    import asyncio
    import time

    from dotenv import dotenv_values
    from livekit import api

    from cli import dispatch as reconcile_dispatch

    root = Path(__file__).resolve().parents[1]
    config = dotenv_values(root / ".env.local")
    env = {
        **os.environ,
        **{key: value for key, value in config.items() if value is not None},
    }
    env["ONBOARDING_DB_PATH"] = str(tmp_path / "room.sqlite3")
    for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.setenv(key, env[key])
    log_path = tmp_path / "worker.log"
    store = Store(env["ONBOARDING_DB_PATH"])
    store.migrate()
    onboarding_id, credential = store.create()
    first_room = "test-" + onboarding_id
    first_connection, _ = store.connect_attempt(
        onboarding_id, credential, first_room, "first-device"
    )
    with log_path.open("w") as log:
        worker = subprocess.Popen(
            [sys.executable, str(root / "agent.py"), "start"],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 30
        while "registered worker" not in log_path.read_text():
            assert worker.poll() is None, log_path.read_text()[-1500:]
            assert time.monotonic() < deadline, log_path.read_text()[-1500:]
            time.sleep(0.1)

        async def dispatch(room, connection_id):
            async with api.LiveKitAPI(
                url=env["LIVEKIT_URL"],
                api_key=env["LIVEKIT_API_KEY"],
                api_secret=env["LIVEKIT_API_SECRET"],
            ) as livekit:
                await livekit.room.create_room(api.CreateRoomRequest(name=room))
                await livekit.agent_dispatch.create_dispatch(
                    api.CreateAgentDispatchRequest(
                        agent_name="resumable-onboarding",
                        room=room,
                        metadata=json.dumps(
                            {
                                "onboarding_id": onboarding_id,
                                "connection_id": connection_id,
                            }
                        ),
                    )
                )

        asyncio.run(dispatch(first_room, first_connection))
        while True:
            first = next(
                c
                for c in store.rows("connections", onboarding_id)
                if c["id"] == first_connection
            )
            if first["job_pid"]:
                break
            assert time.monotonic() < deadline, log_path.read_text()[-1500:]
            time.sleep(0.1)
        assert os.getpgid(first["job_pid"]) == worker.pid
        os.kill(first["job_pid"], signal.SIGKILL)

        while True:
            try:
                os.kill(first["job_pid"], 0)
            except ProcessLookupError:
                break
            assert time.monotonic() < deadline, log_path.read_text()[-1500:]
            time.sleep(0.1)
        asyncio.run(
            reconcile_dispatch(store, onboarding_id, first_connection, first_room)
        )
        while True:
            reclaimed = next(
                c
                for c in store.rows("connections", onboarding_id)
                if c["id"] == first_connection
            )
            if reclaimed["executor_id"] != first["executor_id"]:
                break
            assert time.monotonic() < deadline, log_path.read_text()[-1500:]
            time.sleep(0.1)
        with pytest.raises(PermissionError):
            store.capture(
                onboarding_id,
                first_connection,
                first["executor_id"],
                "stale",
                "stale",
                {},
            )
        second_room = "test-next-" + onboarding_id
        second_connection, generation = store.connect_attempt(
            onboarding_id, credential, second_room, "second-device"
        )
        assert generation == 2
        asyncio.run(dispatch(second_room, second_connection))
        deadline = time.monotonic() + 30
        while True:
            second = next(
                c
                for c in store.rows("connections", onboarding_id)
                if c["id"] == second_connection
            )
            if second["job_pid"] and second["executor_id"]:
                break
            assert time.monotonic() < deadline, log_path.read_text()[-1500:]
            time.sleep(0.1)
        with pytest.raises(PermissionError):
            store.capture(
                onboarding_id,
                first_connection,
                first["executor_id"],
                "late",
                "late",
                {},
            )
        assert store.integrity() == "ok"
        assert store.get(onboarding_id)["revision"] == 0
    finally:
        if worker.poll() is None:
            os.killpg(worker.pid, signal.SIGTERM)
            worker.wait(timeout=10)
