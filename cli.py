"""Trusted local operator commands; resume secrets never enter command arguments."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.request import urlopen

from aiohttp import ClientError
from dotenv import load_dotenv
from livekit import api
from livekit.api.twirp_client import ServerError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from state import Store

load_dotenv(Path(__file__).resolve().parent / ".env.local")
console = Console(stderr=False)


def show(value, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, indent=2, default=str))
    elif isinstance(value, list):
        if not value:
            console.print("No records")
            return
        table = Table(show_lines=True)
        keys = list(value[0])
        for key in keys:
            table.add_column(key)
        for row in value:
            table.add_row(
                *(
                    Text(
                        "".join(
                            ch if ch >= " " or ch in "\n\t" else "�"
                            for ch in str(row.get(key, ""))
                        )
                    )
                    for key in keys
                )
            )
        console.print(table)
    else:
        console.print_json(data=value)


def token(room: str, participant: str) -> str:
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(participant)
        .with_grants(
            api.VideoGrants(
                room_join=True, room=room, can_publish=True, can_subscribe=True
            )
        )
        .with_ttl(timedelta(minutes=15))
        .to_jwt()
    )


async def dispatch(
    store: Store, onboarding_id: str, connection_id: str, room: str
) -> str:
    async with api.LiveKitAPI() as livekit:
        await livekit.room.create_room(api.CreateRoomRequest(name=room))
        existing = await livekit.agent_dispatch.list_dispatch(room)
        metadata = json.dumps(
            {"onboarding_id": onboarding_id, "connection_id": connection_id}
        )
        connection = next(
            row
            for row in store.rows("connections", onboarding_id)
            if row["id"] == connection_id
        )
        for item in existing:
            if item.metadata == metadata:
                process_gone = False
                if connection["job_pid"] is not None:
                    try:
                        os.kill(connection["job_pid"], 0)
                    except ProcessLookupError:
                        process_gone = True
                terminal = item.state.jobs and all(
                    job.state.status in {2, 3} for job in item.state.jobs
                )
                if process_gone or terminal:
                    await livekit.agent_dispatch.delete_dispatch(item.id, room)
                    break
                store.set_dispatch(connection_id, item.id)
                return item.id
        result = await livekit.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=os.getenv("ONBOARDING_AGENT_NAME", "resumable-onboarding"),
                room=room,
                metadata=metadata,
            )
        )
        store.set_dispatch(connection_id, result.id)
        return result.id


async def start_connection(store: Store, onboarding_id: str, credential: str) -> dict:
    participant = f"customer-{uuid.uuid4().hex[:16]}"
    room = f"onboarding-{uuid.uuid4().hex}"
    connection_id, generation = store.connect_attempt(
        onboarding_id, credential, room, participant
    )
    dispatch_id = await dispatch(store, onboarding_id, connection_id, room)
    return {
        "onboarding_id": onboarding_id,
        "connection_id": connection_id,
        "generation": generation,
        "room": room,
        "participant": participant,
        "dispatch_id": dispatch_id,
        "livekit_url": os.environ["LIVEKIT_URL"],
        "token": token(room, participant),
    }


def doctor(store: Store) -> dict:
    url = (
        os.getenv("LIVEKIT_URL", "")
        .replace("ws://", "http://")
        .replace("wss://", "https://")
    )
    reachable = False
    if url:
        try:
            with urlopen(url, timeout=0.5) as response:
                reachable = response.status == 200
        except OSError, ValueError:
            pass
    checks = {
        "offline": {
            "sqlite_version": sqlite3.sqlite_version,
            "wal_reset_fix_version": sqlite3.sqlite_version_info >= (3, 51, 3),
            "integrity": store.integrity(),
        },
        "text_agent": {
            "model_configured": bool(os.getenv("OPENROUTER_MODEL")),
            "openrouter_key": bool(os.getenv("OPENROUTER_API_KEY")),
        },
        "real_room": {
            "url": bool(os.getenv("LIVEKIT_URL")),
            "server_reachable": reachable,
            "api_key": bool(os.getenv("LIVEKIT_API_KEY")),
            "api_secret": bool(os.getenv("LIVEKIT_API_SECRET")),
        },
        "live_voice": {
            "eleven_key": bool(os.getenv("ELEVEN_API_KEY")),
            "voice": bool(os.getenv("ELEVENLABS_VOICE_ID")),
        },
    }
    return checks


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Self-hosted resumable onboarding operator CLI"
    )
    p.add_argument("--json", action="store_true", help="Machine-readable JSON only")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "new", "sessions", "reconcile"):
        sub.add_parser(name)
    for name in (
        "resume",
        "state",
        "transcript",
        "history",
        "connections",
        "operations",
        "followups",
        "summary",
        "export",
        "rotate-resume-credential",
    ):
        command = sub.add_parser(name)
        command.add_argument("onboarding_id")
        if name == "transcript":
            command.add_argument("--role", choices=["user", "assistant"])
            command.add_argument("--connection")
            command.add_argument("--follow", action="store_true")
    backup = sub.add_parser("backup")
    backup.add_argument("target", type=Path)
    return p


def main() -> int:
    args = parser().parse_args()
    store = Store()
    try:
        store.migrate()
        command = args.command
        if command == "doctor":
            value = doctor(store)
        elif command == "new":
            onboarding_id, credential = store.create()
            value = asyncio.run(start_connection(store, onboarding_id, credential))
            value["resume_credential"] = credential
        elif command == "resume":
            credential = getpass.getpass("Resume credential: ")
            value = asyncio.run(start_connection(store, args.onboarding_id, credential))
        elif command == "sessions":
            value = store.list_onboardings()
        elif command == "state":
            row = store.get(args.onboarding_id)
            row.pop("credential_hash")
            row["next_action"] = store.next_action(args.onboarding_id)
            row["pending_inputs"] = len(store.pending_inputs(args.onboarding_id))
            value = row
        elif command in {"transcript", "connections", "operations", "followups"}:
            table = "transcript_events" if command == "transcript" else command

            def records():
                rows = store.rows(table, args.onboarding_id)
                if command == "transcript":
                    rows = [
                        row
                        for row in rows
                        if (not args.role or row["role"] == args.role)
                        and (
                            not args.connection
                            or row["connection_id"] == args.connection
                        )
                    ]
                return rows

            value = records()
            if command == "transcript" and args.follow:
                seen = 0
                while True:
                    rows = records()
                    if len(rows) > seen:
                        show(rows[seen:], args.json)
                    seen = len(rows)
                    time.sleep(1)
        elif command == "history":
            value = [
                row
                for row in store.rows("operations", args.onboarding_id)
                if '"changes":{}' not in (row["result_json"] or "")
            ]
        elif command == "summary":
            value = store.summary(args.onboarding_id)
        elif command == "export":
            row = store.get(args.onboarding_id)
            row.pop("credential_hash")
            value = {
                "state": row,
                "transcript": store.rows("transcript_events", args.onboarding_id),
                "operations": store.rows("operations", args.onboarding_id),
                "followups": store.rows("followups", args.onboarding_id),
                "summary": store.summary(args.onboarding_id),
            }
        elif command == "backup":
            store.backup(args.target)
            value = {
                "backup": str(args.target),
                "integrity": Store(args.target).integrity(),
            }
        elif command == "rotate-resume-credential":
            value = {
                "onboarding_id": args.onboarding_id,
                "resume_credential": store.rotate_credential(args.onboarding_id),
            }
        elif command == "reconcile":
            value = []
            for row in store.list_onboardings():
                if store.next_action(row["id"])["kind"] == "complete":
                    continue
                onboarding_id = row["id"]
                active = [
                    c
                    for c in store.rows("connections", onboarding_id)
                    if c["status"] == "active"
                ]
                for connection in active:
                    dispatch_id = asyncio.run(
                        dispatch(
                            store, onboarding_id, connection["id"], connection["room"]
                        )
                    )
                    value.append(
                        {
                            "connection_id": connection["id"],
                            "dispatch_id": dispatch_id,
                            "pending_inputs": len(store.pending_inputs(onboarding_id)),
                        }
                    )
        else:
            raise AssertionError(command)
        show(value, args.json)
        return 0
    except (RuntimeError, KeyError, ValueError, OSError, ClientError, ServerError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
