"""Durable onboarding policy and SQLite transactions; no LiveKit dependency."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATUSES = {"missing", "answered", "unknown", "declined", "needs_clarification"}
COMPLETE = {"answered", "unknown", "declined"}


def _field(
    field_id: str, question: str, kind: str = "text", required: bool = True
) -> dict:
    return {
        "id": field_id,
        "question": question,
        "kind": kind,
        "required": required,
        "complete_statuses": sorted(COMPLETE),
    }


WORKFLOWS = {
    1: [
        _field("customer.name", "What is your name?"),
        _field(
            "customer.contact", "What is the best email or phone number to reach you?"
        ),
        _field("customer.role", "What is your role?", required=False),
        _field("company.name", "What is your company called?"),
        _field("company.description", "What does your company do?"),
        _field("company.industry", "What industry are you in?", required=False),
        _field(
            "company.employee_count",
            "How many employees do you have?",
            "nonnegative_int",
            False,
        ),
        _field("problem.description", "What problem would you like to solve?"),
        _field(
            "problem.business_impact", "How is this problem affecting your business?"
        ),
        _field("problem.desired_outcome", "What outcome would you like?"),
        _field("urgency.timeframe", "How soon do you need this solved?"),
        _field("urgency.deadline", "Is there a specific deadline?", required=False),
        _field("followup.availability", "When are you available for a follow-up?"),
        _field("followup.customer_timezone", "What time zone are you in?"),
    ],
}
WORKFLOWS[2] = [
    *WORKFLOWS[1],
    _field("company.budget", "What budget range do you have?", required=False),
]

MIGRATIONS = [
    """
    CREATE TABLE onboardings (
      id TEXT PRIMARY KEY, credential_hash TEXT NOT NULL,
      workflow_version INTEGER NOT NULL, workflow_snapshot TEXT NOT NULL,
      state_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
      generation INTEGER NOT NULL DEFAULT 0, active_connection TEXT,
      status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE connections (
      id TEXT PRIMARY KEY, onboarding_id TEXT NOT NULL REFERENCES onboardings(id),
      generation INTEGER NOT NULL, room TEXT NOT NULL, participant TEXT NOT NULL,
      dispatch_id TEXT, job_id TEXT, executor_id TEXT, lease_until TEXT,
      status TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE transcript_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      onboarding_id TEXT NOT NULL REFERENCES onboardings(id),
      connection_id TEXT REFERENCES connections(id), source_id TEXT,
      role TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
      context_json TEXT, related_event INTEGER REFERENCES transcript_events(id),
      created_at TEXT NOT NULL,
      UNIQUE(onboarding_id, connection_id, source_id, kind)
    );
    CREATE INDEX transcript_order ON transcript_events(onboarding_id, id);
    CREATE TABLE operations (
      key TEXT PRIMARY KEY, onboarding_id TEXT NOT NULL REFERENCES onboardings(id),
      source_id TEXT NOT NULL, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
      status TEXT NOT NULL, result_json TEXT, error TEXT, created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX operation_order ON operations(onboarding_id, created_at);
    CREATE TABLE followups (
      id TEXT PRIMARY KEY, onboarding_id TEXT NOT NULL REFERENCES onboardings(id),
      resource TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
      timezone TEXT NOT NULL, proposal_revision INTEGER NOT NULL,
      approval_source TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX active_slot ON followups(resource, start_utc)
      WHERE status = 'booked';
    CREATE INDEX followups_by_session ON followups(onboarding_id, created_at);
    CREATE TABLE summaries (
      onboarding_id TEXT NOT NULL REFERENCES onboardings(id),
      source_revision INTEGER NOT NULL, schema_version INTEGER NOT NULL,
      summary_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
      PRIMARY KEY(onboarding_id, source_revision, schema_version)
    );
    """,
]


class StateError(Exception):
    pass


class Conflict(StateError):
    pass


class Unauthorized(StateError):
    pass


class UnsupportedWorkflow(StateError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


def database_path(value: str | None = None) -> Path:
    raw = Path(value or os.getenv("ONBOARDING_DB_PATH", "data/onboarding.sqlite3"))
    return raw if raw.is_absolute() else ROOT / raw


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = database_path(str(path) if path is not None else None)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.stat().st_mode & 0o077:
            self.path.parent.chmod(0o700)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            settings = {
                key: db.execute(f"PRAGMA {key}").fetchone()[0]
                for key in (
                    "journal_mode",
                    "synchronous",
                    "foreign_keys",
                    "busy_timeout",
                )
            }
            if settings != {
                "journal_mode": "wal",
                "synchronous": 2,
                "foreign_keys": 1,
                "busy_timeout": 5000,
            }:
                raise StateError(f"SQLite safety settings unavailable: {settings}")
            yield db
        finally:
            db.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                db.commit()

    def migrate(self) -> None:
        with self.write() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > len(MIGRATIONS):
                raise StateError(f"Unsupported database schema {version}")
            for index in range(version, len(MIGRATIONS)):
                for statement in MIGRATIONS[index].split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute(f"PRAGMA user_version={index + 1}")
        if self.path.exists():
            self.path.chmod(0o600)

    def create(self, workflow_version: int = 1) -> tuple[str, str]:
        if workflow_version not in WORKFLOWS:
            raise UnsupportedWorkflow(str(workflow_version))
        onboarding_id, credential = uuid.uuid4().hex, secrets.token_urlsafe(32)
        digest = hashlib.sha256(credential.encode()).hexdigest()
        fields = {
            f["id"]: {"status": "missing", "value": None}
            for f in WORKFLOWS[workflow_version]
        }
        with self.write() as db:
            db.execute(
                "INSERT INTO onboardings(id,credential_hash,workflow_version,workflow_snapshot,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    onboarding_id,
                    digest,
                    workflow_version,
                    _json(WORKFLOWS[workflow_version]),
                    _json(fields),
                    now(),
                    now(),
                ),
            )
        return onboarding_id, credential

    def verify_credential(self, onboarding_id: str, credential: str) -> bool:
        row = self.get(onboarding_id)
        return hmac.compare_digest(
            row["credential_hash"], hashlib.sha256(credential.encode()).hexdigest()
        )

    def rotate_credential(self, onboarding_id: str) -> str:
        credential = secrets.token_urlsafe(32)
        with self.write() as db:
            if not db.execute(
                "UPDATE onboardings SET credential_hash=? WHERE id=?",
                (hashlib.sha256(credential.encode()).hexdigest(), onboarding_id),
            ).rowcount:
                raise StateError("Unknown onboarding")
        return credential

    def get(self, onboarding_id: str) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM onboardings WHERE id=?", (onboarding_id,)
            ).fetchone()
        if row is None:
            raise StateError("Unknown onboarding")
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        result["workflow"] = json.loads(result.pop("workflow_snapshot"))
        if result["workflow_version"] not in WORKFLOWS:
            raise UnsupportedWorkflow(str(result["workflow_version"]))
        return result

    def next_action(self, onboarding_id: str) -> dict:
        row = self.get(onboarding_id)
        for field in row["workflow"]:
            item = row["state"].get(field["id"], {"status": "missing"})
            if field["required"] and item["status"] not in field["complete_statuses"]:
                return {
                    "kind": "question",
                    "id": field["id"],
                    "text": field["question"],
                }
        with self.connect() as db:
            booking = db.execute(
                "SELECT * FROM followups WHERE onboarding_id=? AND status='booked' ORDER BY created_at DESC LIMIT 1",
                (onboarding_id,),
            ).fetchone()
            proposal = db.execute(
                "SELECT * FROM followups WHERE onboarding_id=? AND status='proposed' ORDER BY created_at DESC LIMIT 1",
                (onboarding_id,),
            ).fetchone()
        if booking:
            return {
                "kind": "complete",
                "id": "followup.booked",
                "text": "Your follow-up is booked.",
            }
        if proposal:
            return {
                "kind": "approval",
                "id": proposal["id"],
                "text": f"May I book your follow-up for {proposal['start_utc']} UTC?",
            }
        return {
            "kind": "proposal",
            "id": "followup.proposal",
            "text": "I can propose a follow-up time.",
        }

    def connect_attempt(
        self, onboarding_id: str, credential: str, room: str, participant: str
    ) -> tuple[str, int]:
        if not self.verify_credential(onboarding_id, credential):
            raise Unauthorized("Invalid resume credential")
        connection_id = uuid.uuid4().hex
        with self.write() as db:
            row = db.execute(
                "SELECT generation FROM onboardings WHERE id=?", (onboarding_id,)
            ).fetchone()
            generation = row[0] + 1
            db.execute(
                "UPDATE connections SET status='superseded' WHERE onboarding_id=? AND status='active'",
                (onboarding_id,),
            )
            db.execute(
                "INSERT INTO connections(id,onboarding_id,generation,room,participant,status,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    connection_id,
                    onboarding_id,
                    generation,
                    room,
                    participant,
                    "active",
                    now(),
                ),
            )
            db.execute(
                "UPDATE onboardings SET generation=?,active_connection=?,updated_at=? WHERE id=?",
                (generation, connection_id, now(), onboarding_id),
            )
        return connection_id, generation

    def claim(self, connection_id: str, executor_id: str, seconds: int = 60) -> None:
        until = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
        with self.write() as db:
            row = db.execute(
                "SELECT c.*,o.active_connection FROM connections c JOIN onboardings o ON o.id=c.onboarding_id WHERE c.id=?",
                (connection_id,),
            ).fetchone()
            if row is None or row["active_connection"] != connection_id:
                raise Unauthorized("Stale connection")
            if (
                row["executor_id"]
                and row["executor_id"] != executor_id
                and row["lease_until"]
                and row["lease_until"] > now()
            ):
                raise Unauthorized("Executor already owns connection")
            db.execute(
                "UPDATE connections SET executor_id=?,lease_until=? WHERE id=?",
                (executor_id, until, connection_id),
            )

    def _check_owner(
        self,
        db: sqlite3.Connection,
        onboarding_id: str,
        connection_id: str,
        executor_id: str,
    ) -> None:
        row = db.execute(
            "SELECT o.active_connection,c.executor_id,c.lease_until FROM onboardings o JOIN connections c ON c.id=? AND c.onboarding_id=o.id WHERE o.id=?",
            (connection_id, onboarding_id),
        ).fetchone()
        if (
            row is None
            or row["active_connection"] != connection_id
            or row["executor_id"] != executor_id
            or not row["lease_until"]
            or row["lease_until"] <= now()
        ):
            raise Unauthorized("Stale connection or executor")

    def capture(
        self,
        onboarding_id: str,
        connection_id: str,
        executor_id: str,
        source_id: str,
        text: str,
        context: dict,
    ) -> int:
        if not text.strip():
            raise StateError("Empty turn")
        with self.write() as db:
            self._check_owner(db, onboarding_id, connection_id, executor_id)
            db.execute(
                "INSERT OR IGNORE INTO transcript_events(onboarding_id,connection_id,source_id,role,text,kind,context_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    onboarding_id,
                    connection_id,
                    source_id,
                    "user",
                    text,
                    "final_turn",
                    _json(context),
                    now(),
                ),
            )
            row = db.execute(
                "SELECT id,text FROM transcript_events WHERE onboarding_id=? AND connection_id=? AND source_id=? AND kind='final_turn'",
                (onboarding_id, connection_id, source_id),
            ).fetchone()
            if row["text"] != text:
                raise Conflict("Source identity reused with different text")
            return row["id"]

    def _validate(self, field: dict, item: dict) -> dict:
        status = item.get("status", "answered")
        if status not in STATUSES:
            raise StateError(f"Invalid status: {status}")
        value = item.get("value")
        if status == "answered":
            if field["kind"] == "nonnegative_int":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise StateError(f"Invalid value for {field['id']}")
            elif not isinstance(value, str) or not value.strip():
                raise StateError(f"Invalid value for {field['id']}")
            if field["id"] == "followup.customer_timezone":
                try:
                    ZoneInfo(value)
                except KeyError as exc:
                    raise StateError("Invalid timezone") from exc
        elif value is not None:
            raise StateError("Non-answer statuses require null value")
        return {"status": status, "value": value}

    def apply_answers(
        self,
        onboarding_id: str,
        connection_id: str,
        executor_id: str,
        source_id: str,
        answers: dict[str, dict],
        expected_revision: int,
        corrections: list[str] | None = None,
    ) -> dict:
        payload = {
            "answers": answers,
            "expected_revision": expected_revision,
            "corrections": sorted(corrections or []),
        }
        key = f"{onboarding_id}:{source_id}:answers"
        with self.write() as db:
            self._check_owner(db, onboarding_id, connection_id, executor_id)
            if not db.execute(
                "SELECT 1 FROM transcript_events WHERE onboarding_id=? AND source_id=? AND kind='final_turn'",
                (onboarding_id, source_id),
            ).fetchone():
                raise StateError("Input must be durable before interpretation")
            existing = db.execute(
                "SELECT * FROM operations WHERE key=?", (key,)
            ).fetchone()
            if existing:
                if existing["payload_json"] != _json(payload):
                    raise Conflict("Operation key reused with different payload")
                return json.loads(existing["result_json"])
            row = db.execute(
                "SELECT * FROM onboardings WHERE id=?", (onboarding_id,)
            ).fetchone()
            if row["revision"] != expected_revision:
                raise Conflict("Canonical revision changed")
            workflow = json.loads(row["workflow_snapshot"])
            if row["workflow_version"] not in WORKFLOWS:
                raise UnsupportedWorkflow(str(row["workflow_version"]))
            definitions = {f["id"]: f for f in workflow}
            state = json.loads(row["state_json"])
            changes = {}
            for field_id, proposed in answers.items():
                if field_id not in definitions:
                    raise StateError(f"Unknown field: {field_id}")
                accepted = self._validate(definitions[field_id], proposed)
                previous = state[field_id]
                if previous == accepted:
                    continue
                if (
                    previous["status"] == "answered"
                    and field_id not in payload["corrections"]
                ):
                    raise Conflict(
                        f"Changing {field_id} requires explicit correction evidence"
                    )
                changes[field_id] = {"old": previous, "new": accepted}
                state[field_id] = accepted
            revision = row["revision"] + bool(changes)
            if changes:
                db.execute(
                    "UPDATE onboardings SET state_json=?,revision=?,updated_at=? WHERE id=?",
                    (_json(state), revision, now(), onboarding_id),
                )
                db.execute(
                    "UPDATE summaries SET status='historical' WHERE onboarding_id=? AND status='current'",
                    (onboarding_id,),
                )
                if (
                    "followup.availability" in changes
                    or "followup.customer_timezone" in changes
                ):
                    db.execute(
                        "UPDATE followups SET status='invalidated' WHERE onboarding_id=? AND status='proposed'",
                        (onboarding_id,),
                    )
            result = {"revision": revision, "changes": changes}
            db.execute(
                "INSERT INTO operations(key,onboarding_id,source_id,kind,payload_json,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    onboarding_id,
                    source_id,
                    "answers",
                    _json(payload),
                    "committed",
                    _json(result),
                    now(),
                    now(),
                ),
            )
            return result

    def pending_inputs(self, onboarding_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT t.* FROM transcript_events t LEFT JOIN operations o ON o.key=t.onboarding_id||':'||t.source_id||':answers' WHERE t.onboarding_id=? AND t.kind='final_turn' AND o.key IS NULL ORDER BY t.id",
                (onboarding_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def operation(self, onboarding_id: str, source_id: str, kind: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE key=?",
                (f"{onboarding_id}:{source_id}:{kind}",),
            ).fetchone()
        return dict(row) if row else None

    def observe_assistant(
        self,
        onboarding_id: str,
        connection_id: str,
        source_id: str,
        text: str,
        interrupted: bool,
    ) -> None:
        with self.write() as db:
            db.execute(
                "INSERT OR IGNORE INTO transcript_events(onboarding_id,connection_id,source_id,role,text,kind,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    onboarding_id,
                    connection_id,
                    source_id,
                    "assistant",
                    text,
                    "interrupted" if interrupted else "observed",
                    now(),
                ),
            )

    def propose(
        self,
        onboarding_id: str,
        connection_id: str,
        executor_id: str,
        source_id: str,
        start_local: str,
        timezone: str,
        duration_minutes: int = 30,
        resource: str = "followup",
    ) -> dict:
        zone = ZoneInfo(timezone)
        local = datetime.fromisoformat(start_local)
        if local.tzinfo is not None:
            raise StateError("Proposal start must be local wall time")
        aware = local.replace(tzinfo=zone)
        if (
            aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local
            or aware.fold
        ):
            raise StateError("Ambiguous or nonexistent local time")
        start = aware.astimezone(UTC)
        end = start + timedelta(minutes=duration_minutes)
        if start <= datetime.now(UTC):
            raise StateError("Follow-up must be in the future")
        if duration_minutes <= 0:
            raise StateError("Invalid duration")
        hours = os.getenv("FOLLOWUP_WORKING_HOURS", "mon-fri:09:00-17:00")
        days, times = hours.split(":", 1)
        opening, closing = times.split("-")
        weekday_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        first, last = days.split("-")
        allowed_days = {
            weekday_names[index % 7]
            for index in range(
                weekday_names.index(first), weekday_names.index(last) + 1
            )
        }
        org_zone = ZoneInfo(os.getenv("FOLLOWUP_TIMEZONE", "UTC"))
        org_start = start.astimezone(org_zone)
        org_end = end.astimezone(org_zone)
        if (
            weekday_names[org_start.weekday()] not in allowed_days
            or org_start.date() != org_end.date()
            or not (
                opening <= org_start.strftime("%H:%M")
                and org_end.strftime("%H:%M") <= closing
            )
        ):
            raise StateError("Outside configured working hours")
        key = f"{onboarding_id}:{source_id}:proposal"
        payload = {
            "start_local": start_local,
            "timezone": timezone,
            "duration_minutes": duration_minutes,
            "resource": resource,
        }
        with self.write() as db:
            self._check_owner(db, onboarding_id, connection_id, executor_id)
            old = db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if old:
                if old["payload_json"] != _json(payload):
                    raise Conflict("Proposal payload conflict")
                return json.loads(old["result_json"])
            revision = db.execute(
                "SELECT revision FROM onboardings WHERE id=?", (onboarding_id,)
            ).fetchone()[0]
            proposal_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO followups(id,onboarding_id,resource,start_utc,end_utc,timezone,proposal_revision,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    proposal_id,
                    onboarding_id,
                    resource,
                    start.isoformat(),
                    end.isoformat(),
                    timezone,
                    revision,
                    "proposed",
                    now(),
                ),
            )
            result = {
                "id": proposal_id,
                "start_utc": start.isoformat(),
                "end_utc": end.isoformat(),
                "timezone": timezone,
                "revision": revision,
            }
            db.execute(
                "INSERT INTO operations(key,onboarding_id,source_id,kind,payload_json,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    onboarding_id,
                    source_id,
                    "proposal",
                    _json(payload),
                    "committed",
                    _json(result),
                    now(),
                    now(),
                ),
            )
            return result

    def confirm(
        self,
        onboarding_id: str,
        connection_id: str,
        executor_id: str,
        source_id: str,
        proposal_id: str,
        proposal_revision: int,
        approved: bool,
    ) -> dict:
        key = f"{onboarding_id}:{source_id}:booking"
        payload = {
            "proposal_id": proposal_id,
            "proposal_revision": proposal_revision,
            "approved": approved,
        }
        with self.write() as db:
            self._check_owner(db, onboarding_id, connection_id, executor_id)
            old = db.execute("SELECT * FROM operations WHERE key=?", (key,)).fetchone()
            if old:
                if old["payload_json"] != _json(payload):
                    raise Conflict("Booking payload conflict")
                return json.loads(old["result_json"])
            proposal = db.execute(
                "SELECT * FROM followups WHERE id=? AND onboarding_id=?",
                (proposal_id, onboarding_id),
            ).fetchone()
            if (
                proposal is None
                or proposal["status"] != "proposed"
                or proposal["proposal_revision"] != proposal_revision
            ):
                raise Conflict("Proposal is no longer current")
            if not approved:
                db.execute(
                    "UPDATE followups SET status='declined' WHERE id=?", (proposal_id,)
                )
                result = {"status": "declined", "proposal_id": proposal_id}
            else:
                overlap = db.execute(
                    "SELECT id FROM followups WHERE resource=? AND status='booked' AND start_utc<? AND end_utc>? AND onboarding_id<>?",
                    (
                        proposal["resource"],
                        proposal["end_utc"],
                        proposal["start_utc"],
                        onboarding_id,
                    ),
                ).fetchone()
                if overlap:
                    raise Conflict("Slot already booked")
                db.execute(
                    "UPDATE followups SET status='replaced' WHERE onboarding_id=? AND status='booked'",
                    (onboarding_id,),
                )
                db.execute(
                    "UPDATE followups SET status='booked',approval_source=? WHERE id=?",
                    (source_id, proposal_id),
                )
                result = {"status": "booked", "proposal_id": proposal_id}
            db.execute(
                "UPDATE onboardings SET revision=revision+1,updated_at=? WHERE id=?",
                (now(), onboarding_id),
            )
            db.execute(
                "UPDATE summaries SET status='historical' WHERE onboarding_id=? AND status='current'",
                (onboarding_id,),
            )
            db.execute(
                "INSERT INTO operations(key,onboarding_id,source_id,kind,payload_json,status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    onboarding_id,
                    source_id,
                    "booking",
                    _json(payload),
                    "committed",
                    _json(result),
                    now(),
                    now(),
                ),
            )
            return result

    def summary(self, onboarding_id: str) -> dict:
        with self.write() as db:
            row = db.execute(
                "SELECT * FROM onboardings WHERE id=?", (onboarding_id,)
            ).fetchone()
            if row is None:
                raise StateError("Unknown onboarding")
            state = json.loads(row["state_json"])
            booking = db.execute(
                "SELECT * FROM followups WHERE onboarding_id=? AND status='booked' ORDER BY created_at DESC LIMIT 1",
                (onboarding_id,),
            ).fetchone()
            workflow = json.loads(row["workflow_snapshot"])
            unresolved = [
                f["id"]
                for f in workflow
                if f["required"]
                and state[f["id"]]["status"] not in f["complete_statuses"]
            ]
            result = {
                "onboarding_id": onboarding_id,
                "workflow_version": row["workflow_version"],
                "state_revision": row["revision"],
                "summary_schema_version": 1,
                "customer": {
                    k.split(".", 1)[1]: v
                    for k, v in state.items()
                    if k.startswith("customer.")
                },
                "company": {
                    k.split(".", 1)[1]: v
                    for k, v in state.items()
                    if k.startswith("company.")
                },
                "problem": {
                    k.split(".", 1)[1]: v
                    for k, v in state.items()
                    if k.startswith("problem.")
                },
                "urgency": {
                    k.split(".", 1)[1]: v
                    for k, v in state.items()
                    if k.startswith("urgency.")
                },
                "followup": {
                    "fields": {
                        k.split(".", 1)[1]: v
                        for k, v in state.items()
                        if k.startswith("followup.")
                    },
                    "booking": dict(booking) if booking else None,
                },
                "unresolved_items": unresolved,
                "partial": bool(unresolved or not booking),
                "generated_at": now(),
            }
            db.execute(
                "UPDATE summaries SET status='historical' WHERE onboarding_id=? AND status='current' AND source_revision<>?",
                (onboarding_id, row["revision"]),
            )
            db.execute(
                "INSERT OR IGNORE INTO summaries(onboarding_id,source_revision,schema_version,summary_json,status,created_at) VALUES(?,?,?,?,?,?)",
                (
                    onboarding_id,
                    row["revision"],
                    1,
                    _json(result),
                    "current",
                    result["generated_at"],
                ),
            )
            saved = db.execute(
                "SELECT summary_json FROM summaries WHERE onboarding_id=? AND source_revision=? AND schema_version=1",
                (onboarding_id, row["revision"]),
            ).fetchone()[0]
            return json.loads(saved)

    def rows(self, table: str, onboarding_id: str) -> list[dict]:
        if table not in {
            "transcript_events",
            "operations",
            "connections",
            "followups",
            "summaries",
        }:
            raise StateError("Unsupported inspection table")
        order = (
            "id"
            if table in {"transcript_events", "connections", "followups"}
            else "created_at"
        )
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    f"SELECT * FROM {table} WHERE onboarding_id=? ORDER BY {order}",
                    (onboarding_id,),
                )
            ]

    def list_onboardings(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,workflow_version,revision,generation,status,created_at FROM onboardings ORDER BY created_at DESC"
                )
            ]

    def backup(self, target: Path) -> None:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        target.chmod(0o600)

    def integrity(self) -> str:
        with self.connect() as db:
            return db.execute("PRAGMA integrity_check").fetchone()[0]
