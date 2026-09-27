from datetime import UTC, datetime, timedelta

import pytest

from state import Conflict, Store, Unauthorized, UnsupportedWorkflow


@pytest.fixture
def active(tmp_path):
    store = Store(tmp_path / "onboarding.sqlite3")
    store.migrate()
    onboarding_id, credential = store.create()
    connection_id, _ = store.connect_attempt(
        onboarding_id, credential, "room", "person"
    )
    store.claim(connection_id, "executor")
    return store, onboarding_id, credential, connection_id


def capture(active, source, text="I have some information"):
    store, onboarding_id, _, connection_id = active
    store.capture(
        onboarding_id,
        connection_id,
        "executor",
        source,
        text,
        {"question": "customer.name", "captured_at": datetime.now(UTC).isoformat()},
    )


def apply(active, source, answers, corrections=None):
    store, onboarding_id, _, connection_id = active
    return store.apply_answers(
        onboarding_id,
        connection_id,
        "executor",
        source,
        answers,
        store.get(onboarding_id)["revision"],
        corrections,
    )


def test_multi_field_idempotency_and_correction(active):
    store, onboarding_id, _, connection_id = active
    capture(active, "turn1", "I am Ahmed from Atlas, 25 people")
    answers = {
        "customer.name": {"value": "Ahmed"},
        "company.name": {"value": "Atlas"},
        "company.employee_count": {"value": 25},
    }
    first = apply(active, "turn1", answers)
    assert first["revision"] == 1
    assert store.next_action(onboarding_id)["id"] == "customer.contact"
    assert (
        store.apply_answers(
            onboarding_id, connection_id, "executor", "turn1", answers, 0
        )
        == first
    )
    with pytest.raises(Conflict):
        store.apply_answers(
            onboarding_id,
            connection_id,
            "executor",
            "turn1",
            {"customer.name": {"value": "Someone"}},
            0,
        )
    capture(active, "turn2", "Actually we have 50 people")
    second = apply(
        active,
        "turn2",
        {"company.employee_count": {"value": 50}},
        ["company.employee_count"],
    )
    assert second["changes"]["company.employee_count"]["old"]["value"] == 25
    assert store.get(onboarding_id)["state"]["customer.name"]["value"] == "Ahmed"
    assert [row["text"] for row in store.rows("transcript_events", onboarding_id)] == [
        "I am Ahmed from Atlas, 25 people",
        "Actually we have 50 people",
    ]


def test_noop_unknown_and_revision(active):
    store, onboarding_id, _, _ = active
    capture(active, "u")
    assert apply(active, "u", {"customer.name": {"status": "unknown"}})["revision"] == 1
    capture(active, "noop")
    assert apply(active, "noop", {})["revision"] == 1
    assert store.next_action(onboarding_id)["id"] == "customer.contact"


def test_takeover_fences_old_executor(active):
    store, onboarding_id, credential, old_connection = active
    new_connection, generation = store.connect_attempt(
        onboarding_id, credential, "new-room", "new-person"
    )
    assert generation == 2
    store.claim(new_connection, "new-executor")
    with pytest.raises(Unauthorized):
        store.capture(onboarding_id, old_connection, "executor", "late", "late", {})
    assert store.integrity() == "ok"


def test_booking_and_backup(active, tmp_path):
    store, onboarding_id, _, connection_id = active
    start = (
        (datetime.now(UTC) + timedelta(days=8 - datetime.now(UTC).weekday()))
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "")
    )
    proposal = store.propose(
        onboarding_id, connection_id, "executor", "proposal", start, "UTC"
    )
    assert store.next_action(onboarding_id)["kind"] == "question"
    result = store.confirm(
        onboarding_id,
        connection_id,
        "executor",
        "approval",
        proposal["id"],
        proposal["revision"],
        True,
    )
    assert result["status"] == "booked"
    assert (
        store.confirm(
            onboarding_id,
            connection_id,
            "executor",
            "approval",
            proposal["id"],
            proposal["revision"],
            True,
        )
        == result
    )
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    assert Store(backup).integrity() == "ok"
    assert len(Store(backup).rows("followups", onboarding_id)) == 1


def test_workflow_version_and_unsupported(active):
    store, onboarding_id, _, _ = active
    assert "company.budget" not in store.get(onboarding_id)["state"]
    newer_id, _ = store.create(2)
    assert "company.budget" in store.get(newer_id)["state"]
    with store.write() as db:
        db.execute(
            "UPDATE onboardings SET workflow_version=99 WHERE id=?", (onboarding_id,)
        )
    with pytest.raises(UnsupportedWorkflow):
        store.get(onboarding_id)
    assert store.integrity() == "ok"
