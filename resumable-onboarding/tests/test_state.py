from datetime import UTC, datetime, timedelta

import pytest

from state import Conflict, StateError, Store, Unauthorized, UnsupportedWorkflow


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


def test_dead_job_can_reclaim_same_connection(active):
    store, onboarding_id, _, connection_id = active
    with store.write() as db:
        db.execute(
            "UPDATE connections SET job_pid=? WHERE id=?", (2**30, connection_id)
        )
    store.claim(connection_id, "replacement", job_id="new-job", job_pid=2**30 + 1)
    with pytest.raises(Unauthorized):
        store.capture(onboarding_id, connection_id, "executor", "old", "old", {})


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


def next_monday_noon():
    return (
        (datetime.now(UTC) + timedelta(days=8 - datetime.now(UTC).weekday()))
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "")
    )


def test_competing_booking_and_reschedule_preserves_old_on_failure(tmp_path):
    store = Store(tmp_path / "competition.sqlite3")
    store.migrate()
    first_id, first_secret = store.create()
    second_id, second_secret = store.create()
    first_connection, _ = store.connect_attempt(first_id, first_secret, "first", "p1")
    second_connection, _ = store.connect_attempt(
        second_id, second_secret, "second", "p2"
    )
    store.claim(first_connection, "one")
    store.claim(second_connection, "two")
    start = next_monday_noon()
    first = store.propose(first_id, first_connection, "one", "p1", start, "UTC")
    second = store.propose(second_id, second_connection, "two", "p2", start, "UTC")
    store.confirm(
        first_id, first_connection, "one", "a1", first["id"], first["revision"], True
    )
    with pytest.raises(Conflict):
        store.confirm(
            second_id,
            second_connection,
            "two",
            "a2",
            second["id"],
            second["revision"],
            True,
        )
    assert store.rows("followups", second_id)[0]["status"] == "proposed"
    assert store.rows("followups", first_id)[0]["status"] == "booked"
    later = (datetime.fromisoformat(start) + timedelta(days=1)).isoformat()
    other_slot = store.propose(second_id, second_connection, "two", "p3", later, "UTC")
    store.confirm(
        second_id,
        second_connection,
        "two",
        "a3",
        other_slot["id"],
        other_slot["revision"],
        True,
    )
    replacement = store.propose(first_id, first_connection, "one", "p4", later, "UTC")
    with pytest.raises(Conflict):
        store.confirm(
            first_id,
            first_connection,
            "one",
            "a4",
            replacement["id"],
            replacement["revision"],
            True,
        )
    assert (
        next(
            row for row in store.rows("followups", first_id) if row["id"] == first["id"]
        )["status"]
        == "booked"
    )
    assert store.integrity() == "ok"


def test_summary_revision_and_availability_correction_after_booking(active):
    store, onboarding_id, _, connection_id = active
    first_summary = store.summary(onboarding_id)
    assert first_summary["state_revision"] == 0
    capture(active, "availability", "Tuesday works")
    apply(active, "availability", {"followup.availability": {"value": "Tuesday"}})
    assert store.summary(onboarding_id)["state_revision"] == 1
    assert [x["status"] for x in store.rows("summaries", onboarding_id)] == [
        "historical",
        "current",
    ]
    proposal = store.propose(
        onboarding_id, connection_id, "executor", "p1", next_monday_noon(), "UTC"
    )
    store.confirm(
        onboarding_id,
        connection_id,
        "executor",
        "a1",
        proposal["id"],
        proposal["revision"],
        True,
    )
    assert store.get(onboarding_id)["status"] == "complete"
    capture(active, "correction", "Actually Thursday works")
    apply(
        active,
        "correction",
        {"followup.availability": {"value": "Thursday"}},
        ["followup.availability"],
    )
    assert store.get(onboarding_id)["status"] == "reschedule_required"
    assert store.rows("followups", onboarding_id)[0]["status"] == "booked"
    assert store.summary(onboarding_id)["partial"] is True
    assert store.next_action(onboarding_id)["kind"] == "question"


def test_isolation_and_invalid_values(active):
    store, first_id, _, _ = active
    second_id, _ = store.create()
    capture(active, "invalid")
    with pytest.raises(StateError):
        apply(active, "invalid", {"company.employee_count": {"value": False}})
    assert store.get(first_id)["revision"] == 0
    assert store.get(second_id)["revision"] == 0
    assert store.rows("operations", first_id) == []
