"""Start or resume a local customer onboarding console session."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from state import Store

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env.local")


# Check whether the device list has both input and output audio.
def audio_devices_available(output: str) -> bool:
    devices = re.findall(r"^\s*\d+\s+(Input|Output|Both)\s+", output, re.MULTILINE)
    return any(device in {"Input", "Both"} for device in devices) and any(
        device in {"Output", "Both"} for device in devices
    )


# Start or resume a local text or voice onboarding console session.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume", type=Path, metavar="SESSION_FILE")
    parser.add_argument("--voice", action="store_true", help="Use microphone and speaker")
    parser.add_argument("--input-device", help="Microphone index or name substring")
    parser.add_argument("--output-device", help="Speaker index or name substring")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices")
    args = parser.parse_args()
    lk_bin = os.getenv("ONBOARDING_LK_BIN", "lk")

    if args.list_devices:
        return subprocess.call([lk_bin, "agent", "console", "--list-devices"])
    if not args.voice and (args.input_device or args.output_device):
        parser.error("audio device options require --voice")
    if args.voice:
        for name in ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "ELEVEN_API_KEY", "ELEVENLABS_VOICE_ID"):
            if not os.getenv(name):
                parser.error(f"{name} must be configured for voice mode")
        devices = subprocess.run(
            [lk_bin, "agent", "console", "--list-devices"],
            capture_output=True,
            text=True,
            check=False,
        )
        if devices.returncode or not audio_devices_available(devices.stdout):
            parser.error(
                "No usable microphone and speaker were found. Run "
                "./scripts/start-session.sh --list-devices to inspect audio devices."
            )

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
    if args.voice:
        print("Speak as the customer. Press Ctrl+C to leave; use --resume --voice to continue.")
    else:
        print("Type as the customer. Press Ctrl+C to leave; use --resume to continue.")
    env = os.environ.copy()
    env.update(
        ONBOARDING_CONSOLE_ID=session["onboarding_id"],
        ONBOARDING_CONSOLE_CREDENTIAL=session["resume_credential"],
        ONBOARDING_TEXT_ONLY="0" if args.voice else "1",
    )
    uv_bin = Path(env.get("UV_BIN", "uv"))
    if uv_bin.parent != Path("."):
        env["PATH"] = f"{uv_bin.parent}:{env.get('PATH', '')}"
    try:
        command = [lk_bin, "agent", "console"]
        if not args.voice:
            command.append("--text")
        if args.input_device:
            command.extend(["--input-device", args.input_device])
        if args.output_device:
            command.extend(["--output-device", args.output_device])
        command.append("agent.py")
        return subprocess.call(
            command, cwd=ROOT, env=env
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
