#!/usr/bin/env python3
"""Read-only bearer-authenticated host telemetry endpoint for a homelab node."""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from node_telemetry import HostTelemetryCollector

HOST = os.getenv("NODE_TELEMETRY_HOST", "127.0.0.1")
PORT = int(os.getenv("NODE_TELEMETRY_PORT", "8767"))
TOKEN = os.getenv("NODE_TELEMETRY_TOKEN", "")


class State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.data: dict = {}
        self.updated_at = 0.0
        self.message = "Waiting for first sample"

    def update(self, data: dict) -> None:
        with self.lock:
            self.data = data
            self.updated_at = time.time()
            self.message = ""

    def snapshot(self) -> tuple[dict, float, str]:
        with self.lock:
            return deepcopy(self.data), self.updated_at, self.message


STATE = State()


def sampler() -> None:
    collector = HostTelemetryCollector()
    while True:
        try:
            STATE.update(collector.snapshot())
        except Exception as exc:
            with STATE.lock:
                STATE.message = "Telemetry sampling failed"
        time.sleep(2)


def respond(handler: BaseHTTPRequestHandler, code: int, status: str, data: object, message: str = "") -> None:
    encoded = json.dumps({"status": status, "data": data, "updated_at": time.time(), "message": message}).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(encoded)


class Handler(BaseHTTPRequestHandler):
    server_version = "NodeTelemetryAgent/1.0"

    def log_message(self, *_: object) -> None:
        return

    def authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return bool(TOKEN) and header.startswith("Bearer ") and secrets.compare_digest(header[7:], TOKEN)

    def do_GET(self) -> None:
        if urlparse(self.path).path != "/v1/telemetry":
            respond(self, HTTPStatus.NOT_FOUND, "error", {}, "Unknown endpoint")
        elif not self.authorized():
            respond(self, HTTPStatus.UNAUTHORIZED, "error", {}, "Unauthorized")
        else:
            data, updated_at, message = STATE.snapshot()
            respond(self, HTTPStatus.OK, "ok" if data else "loading", data, message or ("" if updated_at else "Waiting for first sample"))


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("NODE_TELEMETRY_TOKEN must be set")
    threading.Thread(target=sampler, daemon=True, name="node-telemetry-sampler").start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
