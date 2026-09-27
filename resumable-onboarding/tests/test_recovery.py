"""Real SIGKILL checkpoints against a temporary SQLite database."""

import os
import select
import signal
import subprocess
import sys
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
    print("CHECKPOINT", flush=True)
    signal.pause()


@pytest.mark.parametrize("stage", ["after_input", "inside_transaction", "after_state"])
def test_sigkill_recovery(tmp_path, stage):
    path = tmp_path / "crash.sqlite3"
    store = Store(path)
    store.migrate()
    onboarding_id, credential = store.create()
    connection_id, _ = store.connect_attempt(
        onboarding_id, credential, "room", "person"
    )
    store.claim(connection_id, "executor")
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
    assert len(reopened.rows("transcript_events", onboarding_id)) == 1
    if stage == "after_state":
        assert reopened.get(onboarding_id)["state"]["customer.name"]["value"] == "Ahmed"
        assert reopened.pending_inputs(onboarding_id) == []
        assert reopened.next_action(onboarding_id)["id"] == "customer.contact"
        assert len(reopened.rows("operations", onboarding_id)) == 1
    else:
        assert reopened.get(onboarding_id)["revision"] == 0
        assert len(reopened.pending_inputs(onboarding_id)) == 1
        assert reopened.next_action(onboarding_id)["id"] == "customer.name"
        assert reopened.rows("operations", onboarding_id) == []


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "worker":
    worker(*sys.argv[2:])
