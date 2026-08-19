#!/usr/bin/env python3
"""Step-by-step Action Logger.

Monitors the active X11 window title, focused elements, and timestamps,
recording every user step into a persistent log file.
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = _ROOT / "logs" / "user_session_steps.txt"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

DISPLAY = os.environ.get("DISPLAY", ":2")


def get_active_window_title() -> str:
    try:
        res = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True,
            text=True,
            check=False,
            env=dict(os.environ, DISPLAY=DISPLAY),
        )
        return res.stdout.strip()
    except Exception:
        return ""


def main():
    print(f"[*] Starting Step Logger on Display {DISPLAY}...")
    print(f"[*] Logging actions to: {LOG_FILE}")

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n\n=== SESSION RECORDING STARTED AT {datetime.datetime.now().isoformat()} ===\n")
        f.flush()

        last_title = ""
        while True:
            try:
                title = get_active_window_title()
                if title and title != last_title:
                    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                    entry = f"[{timestamp}] [Window Change] Active Window: {title}\n"
                    f.write(entry)
                    f.flush()
                    print(entry.strip())
                    last_title = title
                time.sleep(1.0)
            except KeyboardInterrupt:
                break
            except Exception as e:
                time.sleep(1.0)


if __name__ == "__main__":
    main()
