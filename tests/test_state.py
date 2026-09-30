from datetime import UTC, datetime, timedelta
from stat import S_IMODE

import pytest

from state import Store


# Create an active onboarding and claimed connection for tests.
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


# Verify opening an existing database preserves directory permissions.
def test_existing_database_directory_permissions_are_preserved(tmp_path):
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir(mode=0o755)
    store = Store(shared_dir / "onboarding.sqlite3")

    store.migrate()

    assert S_IMODE(shared_dir.stat().st_mode) == 0o755
    assert S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.integrity() == "ok"


# Persist a test customer turn and return its source ID.
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


# Apply test answers to the active onboarding.
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


# Verify multiple answers, replay safety, and explicit corrections.
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
    with pytest.raises(ValueError):
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


# Verify no-op and unknown answers handle revisions correctly.
def test_noop_unknown_and_revision(active):
    store, onboarding_id, _, _ = active
    capture(active, "u")
    assert apply(active, "u", {"customer.name": {"status": "unknown"}})["revision"] == 1
    capture(active, "noop")
    assert apply(active, "noop", {})["revision"] == 1
    assert store.next_action(onboarding_id)["id"] == "customer.contact"


# Verify a new executor prevents writes from the old one.
def test_takeover_fences_old_executor(active):
    store, onboarding_id, credential, old_connection = active
    new_connection, generation = store.connect_attempt(
        onboarding_id, credential, "new-room", "new-person"
    )
    assert generation == 2
    store.claim(new_connection, "new-executor")
    with pytest.raises(PermissionError):
        store.capture(onboarding_id, old_connection, "executor", "late", "late", {})
    assert store.integrity() == "ok"


# Verify a dead job can reclaim its connection.
def test_dead_job_can_reclaim_same_connection(active):
    store, onboarding_id, _, connection_id = active
    with store.write() as db:
        db.execute(
            "UPDATE connections SET job_pid=? WHERE id=?", (2**30, connection_id)
        )
    store.claim(connection_id, "replacement", job_id="new-job", job_pid=2**30 + 1)
    with pytest.raises(PermissionError):
        store.capture(onboarding_id, connection_id, "executor", "old", "old", {})


# Verify follow-up booking and database backup.
def test_booking_and_backup(active, tmp_path):
    store, onboarding_id, _, connection_id = active
    start = (
        (datetime.now(UTC) + timedelta(days=8 - datetime.now(UTC).weekday()))
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "")
    )
    proposal = prepared_proposal(
        store, onboarding_id, connection_id, "executor", "proposal", start
    )
    assert store.next_action(onboarding_id)["kind"] == "question"
    result = approval(
        store, onboarding_id, connection_id, "executor", "approval", proposal
    )
    assert result["status"] == "booked"
    assert (
        approval(store, onboarding_id, connection_id, "executor", "approval", proposal)
        == result
    )
    backup = tmp_path / "backup.sqlite3"
    store.backup(backup)
    assert Store(backup).integrity() == "ok"
    assert len(Store(backup).rows("followups", onboarding_id)) == 1


# Verify workflow versions and unsupported versions are handled.
def test_workflow_version_and_unsupported(active):
    store, onboarding_id, _, _ = active
    assert "company.budget" not in store.get(onboarding_id)["state"]
    newer_id, _ = store.create(2)
    assert "company.budget" in store.get(newer_id)["state"]
    with store.write() as db:
        db.execute(
            "UPDATE onboardings SET workflow_version=99 WHERE id=?", (onboarding_id,)
        )
    with pytest.raises(ValueError):
        store.get(onboarding_id)
    assert store.integrity() == "ok"


# Return a future Monday at noon for booking tests.
def next_monday_noon():
    return (
        (datetime.now(UTC) + timedelta(days=8 - datetime.now(UTC).weekday()))
        .replace(hour=12, minute=0, second=0, microsecond=0)
        .isoformat()
        .replace("+00:00", "")
    )


# Create an observed follow-up proposal for a test.
def prepared_proposal(store, onboarding_id, connection_id, executor, source, start):
    store.capture(
        onboarding_id,
        connection_id,
        executor,
        source,
        f"I am available {start}",
        {"action": {"kind": "proposal"}},
    )
    return store.propose(onboarding_id, connection_id, executor, source, start, "UTC")


# Capture a test customer response to a proposal.
def approval(store, onboarding_id, connection_id, executor, source, proposal):
    text = f"May I book your follow-up for {proposal['start_utc']} UTC?"
    store.observe_assistant(onboarding_id, connection_id, f"ask-{source}", text, False)
    store.capture(
        onboarding_id,
        connection_id,
        executor,
        source,
        "yes",
        {
            "action": {
                "kind": "approval",
                "id": proposal["id"],
                "revision": proposal["revision"],
                "text": text,
            }
        },
    )
    return store.confirm(
        onboarding_id,
        connection_id,
        executor,
        source,
        proposal["id"],
        proposal["revision"],
        True,
    )


# Verify booking conflicts leave an earlier reservation intact.
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
    first = prepared_proposal(store, first_id, first_connection, "one", "p1", start)
    second = prepared_proposal(store, second_id, second_connection, "two", "p2", start)
    approval(store, first_id, first_connection, "one", "a1", first)
    with pytest.raises(ValueError):
        approval(store, second_id, second_connection, "two", "a2", second)
    assert store.rows("followups", second_id)[0]["status"] == "proposed"
    assert store.rows("followups", first_id)[0]["status"] == "booked"
    later = (datetime.fromisoformat(start) + timedelta(days=1)).isoformat()
    other_slot = prepared_proposal(
        store, second_id, second_connection, "two", "p3", later
    )
    approval(store, second_id, second_connection, "two", "a3", other_slot)
    replacement = prepared_proposal(
        store, first_id, first_connection, "one", "p4", later
    )
    with pytest.raises(ValueError):
        approval(store, first_id, first_connection, "one", "a4", replacement)
    assert (
        next(
            row for row in store.rows("followups", first_id) if row["id"] == first["id"]
        )["status"]
        == "booked"
    )
    assert store.integrity() == "ok"


# Verify summaries refresh after booking and availability changes.
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
    proposal = prepared_proposal(
        store, onboarding_id, connection_id, "executor", "p1", next_monday_noon()
    )
    approval(store, onboarding_id, connection_id, "executor", "a1", proposal)
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


# Verify onboarding isolation and rejection of invalid answers.
def test_isolation_and_invalid_values(active):
    store, first_id, _, _ = active
    second_id, _ = store.create()
    capture(active, "invalid")
    with pytest.raises(RuntimeError):
        apply(active, "invalid", {"company.employee_count": {"value": False}})
    assert store.get(first_id)["revision"] == 0
    assert store.get(second_id)["revision"] == 0
    assert store.rows("operations", first_id) == []


# Verify booking needs the exact proposal shown to the customer.
def test_booking_requires_observed_exact_proposal(active):
    store, onboarding_id, _, connection_id = active
    proposal = prepared_proposal(
        store, onboarding_id, connection_id, "executor", "p1", next_monday_noon()
    )
    text = f"May I book your follow-up for {proposal['start_utc']} UTC?"
    store.capture(
        onboarding_id,
        connection_id,
        "executor",
        "yes",
        "yes",
        {
            "action": {
                "kind": "approval",
                "id": proposal["id"],
                "revision": proposal["revision"],
                "text": text,
            }
        },
    )
    with pytest.raises(ValueError, match="not observed"):
        store.confirm(
            onboarding_id,
            connection_id,
            "executor",
            "yes",
            proposal["id"],
            proposal["revision"],
            True,
        )
    assert store.rows("followups", onboarding_id)[0]["status"] == "proposed"
    store.observe_assistant(onboarding_id, connection_id, "too-late", text, False)
    with pytest.raises(ValueError, match="not observed"):
        store.confirm(
            onboarding_id,
            connection_id,
            "executor",
            "yes",
            proposal["id"],
            proposal["revision"],
            True,
        )


# Verify declining follow-up can finish without a timezone.
def test_declined_followup_finishes_without_timezone(active):
    store, onboarding_id, _, _ = active
    capture(active, "decline", "I do not want a follow-up")
    answers = {
        field["id"]: {"value": field["id"]}
        for field in store.get(onboarding_id)["workflow"]
        if field["required"]
        and field["id"] not in {"followup.availability", "followup.customer_timezone"}
    }
    answers["followup.availability"] = {"status": "declined"}
    apply(active, "decline", answers)
    assert store.next_action(onboarding_id)["kind"] == "review_offer"
    assert store.summary(onboarding_id)["partial"] is False


# Verify zero values and busy writes keep input recoverable.
def test_zero_value_and_busy_write_preserve_pending_input(active):
    import sqlite3

    store, onboarding_id, _, connection_id = active
    capture(active, "zero", "We have zero employees")
    result = apply(active, "zero", {"company.employee_count": {"value": 0}})
    assert result["revision"] == 1
    assert store.get(onboarding_id)["state"]["company.employee_count"]["value"] == 0
    capture(active, "busy", "My name is Ahmed")
    blocker = sqlite3.connect(store.path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            store.apply_answers(
                onboarding_id,
                connection_id,
                "executor",
                "busy",
                {"customer.name": {"value": "Ahmed"}},
                1,
            )
    finally:
        blocker.rollback()
        blocker.close()
    assert store.get(onboarding_id)["revision"] == 1
    assert [item["source_id"] for item in store.pending_inputs(onboarding_id)] == [
        "busy"
    ]


# Verify review choices and corrections survive a new Store instance.
def test_review_walks_answers_and_applies_correction(active):
    store, onboarding_id, _, connection_id = active
    capture(active, "initial", "I am Ahmed and do not want a follow-up")
    answers = {
        field["id"]: {"value": field["id"]}
        for field in store.get(onboarding_id)["workflow"]
        if field["required"]
        and field["id"] not in {"followup.availability", "followup.customer_timezone"}
    }
    answers["followup.availability"] = {"status": "declined"}
    apply(active, "initial", answers)
    assert store.next_action(onboarding_id)["kind"] == "review_offer"

    def decide(source, utterance, decision):
        action = store.next_action(onboarding_id)
        store.capture(
            onboarding_id,
            connection_id,
            "executor",
            source,
            utterance,
            {"action": action},
        )
        return store.review_decision(
            onboarding_id, connection_id, "executor", source, decision
        )

    started = decide("start-review", "Yes, please", "start")
    assert (
        store.review_decision(
            onboarding_id, connection_id, "executor", "start-review", "start"
        )
        == started
    )
    reopened = Store(store.path)
    assert reopened.next_action(onboarding_id)["id"] == "customer.name"
    decide("reject-name", "That is wrong", "change")
    assert reopened.next_action(onboarding_id)["kind"] == "review_correction"
    action = reopened.next_action(onboarding_id)
    store.capture(
        onboarding_id,
        connection_id,
        "executor",
        "unclear-fix",
        "I need to think",
        {"action": action},
    )
    apply(active, "unclear-fix", {})
    assert reopened.next_action(onboarding_id)["kind"] == "review_correction"
    store.capture(
        onboarding_id,
        connection_id,
        "executor",
        "fix-name",
        "My name is Sara",
        {"action": action},
    )
    result = store.apply_answers(
        onboarding_id,
        connection_id,
        "executor",
        "fix-name",
        {"customer.name": {"value": "Sara"}},
        store.get(onboarding_id)["revision"],
        ["customer.name"],
    )
    assert result["changes"]["customer.name"]["new"]["value"] == "Sara"
    assert reopened.next_action(onboarding_id)["id"] == "customer.contact"
    assert reopened.pending_inputs(onboarding_id) == []
    for index in range(len(answers) - 1):
        decide(f"accept-{index}", "That is correct", "accept")
    assert reopened.next_action(onboarding_id)["kind"] == "complete"
    assert reopened.get(onboarding_id)["state"]["customer.name"]["value"] == "Sara"
