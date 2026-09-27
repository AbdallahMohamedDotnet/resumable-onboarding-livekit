"""Start or resume a local text-only customer onboarding session."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from state import Store

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env.local")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path, metavar="SESSION_FILE")
    args = parser.parse_args()

    store = Store()
    store.migrate()
    if args.resume:
        session_file = args.resume.resolve()
        session = json.loads(session_file.read_text())
    else:
        onboarding_id, credential = store.create()
        session = {"onboarding_id": onboarding_id, "resume_credential": credential}
        run_dir = ROOT / "run"
        run_dir.mkdir(mode=0o700, exist_ok=True)
        session_file = run_dir / f"taker-{onboarding_id}.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(session_file, flags, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(session, output)
            output.write("\n")

    print(f"Onboarding ID: {session['onboarding_id']}")
    print(f"Resume file: {session_file}")
    print("Type as the customer. Press Ctrl+C to leave; use --resume to continue.")
    env = os.environ.copy()
    env.update(
        ONBOARDING_CONSOLE_ID=session["onboarding_id"],
        ONBOARDING_CONSOLE_CREDENTIAL=session["resume_credential"],
        ONBOARDING_TEXT_ONLY="1",
    )
    uv_bin = Path(env.get("UV_BIN", "uv"))
    if uv_bin.parent != Path("."):
        env["PATH"] = f"{uv_bin.parent}:{env.get('PATH', '')}"
    try:
        return subprocess.call(
            ["lk", "agent", "console", "--text", "agent.py"], cwd=ROOT, env=env
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
