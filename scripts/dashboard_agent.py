#!/usr/bin/env python3
"""Restricted, token-authenticated status/control API for the kiosk dashboard.

Run this on the Dell. It exposes only cached status and fixed maintenance
operations; it never accepts an arbitrary shell command, path, or unit name.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from collections import deque
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from node_telemetry import HostTelemetryCollector

SOURCE_WORK_DIR = Path(__file__).resolve().parents[1]
RUNTIME_DIR = Path(os.getenv("PAB_RUNTIME_DIR", str(SOURCE_WORK_DIR)))
_configured_work_dir = Path(os.getenv("PAB_WORKDIR", str(SOURCE_WORK_DIR)))
# A stale staging path can leave the dashboard agent querying a checkout with
# no private configuration, even though it is executed from the live bot.
# Prefer that live source directory unless the explicitly configured workdir
# has its own private environment file.
WORK_DIR = _configured_work_dir if (_configured_work_dir / ".env").is_file() else SOURCE_WORK_DIR
HOST = os.getenv("PAB_DASHBOARD_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("PAB_DASHBOARD_AGENT_PORT", "8765"))
TOKEN = os.getenv("PAB_DASHBOARD_AGENT_TOKEN", "")
MAX_REQUEST_BYTES = 4 * 1024
ACTION_RUNNER = os.getenv("PAB_DASHBOARD_ACTION_RUNNER", "/usr/local/libexec/pab-dashboard-action")
ALLOWED_ACTIONS = {"bot-start", "bot-stop", "bot-restart", "health-check", "daily-digest", "caldav-restart"}


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.data: dict = {"service": {"state": "loading"}, "host": {}, "route": {}, "tasks": [], "jobs": []}
        self.jobs: dict[str, dict] = {}

    def snapshot(self) -> dict:
        with self.lock:
            data = deepcopy(self.data)
            data["jobs"] = sorted((deepcopy(job) for job in self.jobs.values()), key=lambda item: item["updated_at"], reverse=True)
            return data

    def update(self, **values: object) -> None:
        with self.lock:
            self.data.update(values)


STATE = State()


def command(args: list[str], timeout: int = 5) -> tuple[bool, str]:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode == 0, (result.stdout or result.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)


def service_status(unit: str) -> dict:
    ok, output = command([
        "systemctl", "show", unit,
        "--property=ActiveState", "--property=SubState",
        "--property=ActiveEnterTimestamp", "--property=MainPID",
    ], 4)
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return {
        "unit": unit,
        "state": values.get("ActiveState", "unknown") if ok else "unknown",
        "substate": values.get("SubState", ""),
        "started_at": values.get("ActiveEnterTimestamp", ""),
        "pid": values.get("MainPID", ""),
    }


def read_route() -> dict:
    try:
        return json.loads((RUNTIME_DIR / "dashboard_route_state.json").read_text())
    except (OSError, ValueError):
        return {}


def read_tasks() -> list[dict]:
    """Read active assignment tasks, retaining unfinished overdue work.

    The dashboard is an action queue, not a calendar archive.  Hiding an
    overdue item makes the most important unfinished work disappear entirely,
    so leave it in the snapshot and flag it for the UI.
    """
    try:
        sys.path.insert(0, str(WORK_DIR))
        from scrapers.assignment_calendar import collect_assignments
        items = collect_assignments()
        rows = []
        for item in items:
            if getattr(item, "completed", False):
                continue
            due = str(getattr(item, "due_date", ""))[:32]
            overdue = False
            try:
                overdue = date.fromisoformat(due[:10]) < date.today()
            except ValueError:
                pass
            due_label = f"{due} · overdue" if overdue else due
            rows.append({"title": str(getattr(item, "title", "Task"))[:140], "course": str(getattr(item, "course", ""))[:80], "source": str(getattr(item, "source", ""))[:40], "due": due_label, "sort_due": due, "overdue": overdue, "official": bool(getattr(item, "official", False))})
        return sorted(rows, key=lambda row: (not row["overdue"], row["sort_due"] or "9999"))[:20]
    except Exception:
        return []


def status_loop() -> None:
    task_due = 0.0
    service_due = 0.0
    host_due = 0.0
    host = HostTelemetryCollector()
    while True:
        current = time.time()
        if current >= service_due:
            STATE.update(service=service_status("bot.service"), caldav=service_status("assignment-caldav.service"), route=read_route(), updated_at=current)
            service_due = current + 5
        if current >= host_due:
            try:
                STATE.update(host=host.snapshot())
            except Exception:
                STATE.update(host={"error": "telemetry unavailable"})
            host_due = current + 2
        if time.time() >= task_due:
            STATE.update(tasks=read_tasks())
            task_due = time.time() + 60
        time.sleep(1)


def run_action(action: str) -> None:
    with STATE.lock:
        job = STATE.jobs[action]
        job.update(state="running", updated_at=time.time(), message="Running")
    ok, _output = command(["sudo", ACTION_RUNNER, action], 25)
    with STATE.lock:
        job.update(
            state="success" if ok else "failed",
            updated_at=time.time(),
            message="Completed" if ok else "Action failed; check local service logs.",
        )


def launch_action(action: str) -> tuple[bool, dict, str]:
    if action not in ALLOWED_ACTIONS:
        return False, {}, "Unknown action"
    with STATE.lock:
        old = STATE.jobs.get(action)
        if old and old.get("state") == "running":
            return False, deepcopy(old), "This action is already running"
        STATE.jobs[action] = {"name": action, "state": "queued", "updated_at": time.time(), "message": "Queued"}
        job = deepcopy(STATE.jobs[action])
    threading.Thread(target=run_action, args=(action,), daemon=True, name=f"dashboard-{action}").start()
    return True, job, "Action accepted"


def response(handler: BaseHTTPRequestHandler, code: int, status: str, data: object, message: str = "") -> None:
    encoded = json.dumps({"status": status, "data": data, "updated_at": time.time(), "message": message}).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(encoded)


class Handler(BaseHTTPRequestHandler):
    server_version = "PABDashboardAgent/1.0"

    def log_message(self, *_: object) -> None:
        return

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return bool(TOKEN) and header.startswith("Bearer ") and secrets.compare_digest(header[7:], TOKEN)

    def request_too_large(self) -> bool:
        try:
            return int(self.headers.get("Content-Length", "0")) > MAX_REQUEST_BYTES
        except ValueError:
            return True

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/v1/status":
            response(self, HTTPStatus.NOT_FOUND, "error", {}, "Unknown endpoint")
        elif not self.authorized():
            response(self, HTTPStatus.UNAUTHORIZED, "error", {}, "Unauthorized")
        else:
            response(self, HTTPStatus.OK, "ok", STATE.snapshot())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        prefix = "/v1/actions/"
        if self.request_too_large():
            response(self, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "error", {}, "Request too large")
        elif not path.startswith(prefix):
            response(self, HTTPStatus.NOT_FOUND, "error", {}, "Unknown endpoint")
        elif not self.authorized():
            response(self, HTTPStatus.UNAUTHORIZED, "error", {}, "Unauthorized")
        else:
            ok, job, message = launch_action(path[len(prefix):])
            response(self, HTTPStatus.ACCEPTED if ok else HTTPStatus.CONFLICT, "accepted" if ok else "error", job, message)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("PAB_DASHBOARD_AGENT_TOKEN must be set")
    threading.Thread(target=status_loop, daemon=True, name="dashboard-status").start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
