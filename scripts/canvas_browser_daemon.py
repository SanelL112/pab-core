#!/usr/bin/env python3
"""Keep one VNC-visible Firefox session alive for Canvas ClassLink access.

Run this with ``DISPLAY=:1`` after starting VNC.  Sign into ClassLink manually
in the Firefox window; the normal scraper then sends read-only requests to this
process over a localhost-only HTTP endpoint.  Passwords, cookies, and browser
storage never leave Firefox.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.canvas_scraper import (  # noqa: E402
    CANVAS_API_URL,
    CLASSLINK_URL,
    CanvasBrowserClient,
    CanvasSessionError,
    CanvasSignInRequired,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("canvas_browser_daemon")


class VirtualDisplay:
    """Provide a regular Firefox display for unattended system-service use."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if os.environ.get("DISPLAY"):
            return
        binary = shutil.which("Xvfb")
        if not binary:
            raise RuntimeError("DISPLAY is not set and Xvfb is not installed.")
        display = os.getenv("CANVAS_VIRTUAL_DISPLAY", ":99")
        self.process = subprocess.Popen(
            [binary, display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.4)
        if self.process.poll() is not None:
            raise RuntimeError("Could not start the Canvas virtual display.")
        os.environ["DISPLAY"] = display

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


class BrowserDaemon:
    def __init__(self) -> None:
        self.client = CanvasBrowserClient(headless=False, use_daemon=False)
        self.lock = threading.Lock()
        self.was_authenticated = False
        self.last_reauth_attempt = 0.0
        self.reauth_cooldown = int(os.getenv("CANVAS_REAUTH_COOLDOWN_SECONDS", "900"))

    def start(self) -> None:
        self.client._start_browser()
        assert self.client.driver is not None
        self.client.driver.get(CLASSLINK_URL)

    def location(self) -> str:
        assert self.client.driver is not None
        return self.client._safe_browser_location()

    def _select_canvas_tab(self) -> bool:
        """Select the Canvas tab when ClassLink opens it in a new window/tab."""
        assert self.client.driver is not None
        driver = self.client.driver
        original = driver.current_window_handle
        canvas_host = urlsplit(CANVAS_API_URL).netloc
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            if urlsplit(driver.current_url).netloc == canvas_host:
                return True
        driver.switch_to.window(original)
        return False

    def health(self) -> dict[str, object]:
        with self.lock:
            assert self.client.driver is not None
            self._select_canvas_tab()
            host = urlsplit(self.client.driver.current_url).netloc
            if not host.endswith("instructure.com"):
                return {"authenticated": False, "location": self.location()}
            try:
                user = self.client.get_current_user()
                authenticated = bool(user.get("id"))
                self.was_authenticated = self.was_authenticated or authenticated
                return {"authenticated": authenticated, "location": self.location()}
            except CanvasSessionError:
                return {"authenticated": False, "location": self.location()}

    def auto_reauthenticate(self) -> None:
        """Run the ordinary ClassLink → ADFS → Canvas flow without a manual bootstrap."""
        if not os.getenv("CLASSLINK_USERNAME") or not os.getenv("CLASSLINK_PASSWORD"):
            logger.warning("Automatic ClassLink sign-in is disabled: credentials are not configured")
            return
        if time.monotonic() - self.last_reauth_attempt < self.reauth_cooldown:
            return

        self.last_reauth_attempt = time.monotonic()
        logger.info("Attempting normal ClassLink → ADFS → Canvas authentication")
        try:
            with self.lock:
                self.client._sign_in_via_classlink()
                if not self.client._is_canvas_authenticated():
                    logger.warning("Automatic ClassLink authentication did not reach Canvas; inspect the VNC browser window")
        except CanvasSessionError as exc:
            logger.warning("Automatic ClassLink authentication needs attention in VNC: %s", exc)
        except Exception:
            logger.exception("Automatic ClassLink reauthentication failed")

    def request(self, path_or_url: str) -> tuple[object, str]:
        target = urlsplit(path_or_url)
        canvas_host = urlsplit(CANVAS_API_URL).netloc
        if target.netloc and target.netloc != canvas_host:
            raise CanvasSessionError("Only the configured Canvas domain may be requested.")
        if not target.path.startswith("/api/v1/"):
            raise CanvasSessionError("Only Canvas API v1 paths may be requested.")
        with self.lock:
            if not self._select_canvas_tab():
                raise CanvasSignInRequired("Canvas is not open in the persistent Firefox session.")
            return self.client._request_json(path_or_url)

    def close(self) -> None:
        self.client.close()


class DaemonHandler(BaseHTTPRequestHandler):
    server: "CanvasDaemonServer"

    def do_GET(self) -> None:  # noqa: N802
        route = urlsplit(self.path)
        if route.path == "/health":
            self._send(200, self.server.daemon.health())
            return
        if route.path == "/request":
            value = parse_qs(route.query).get("path", [""])[0]
            try:
                data, link = self.server.daemon.request(value)
            except CanvasSignInRequired as exc:
                self._send(401, {"error": str(exc)})
            except CanvasSessionError as exc:
                self._send(400, {"error": str(exc)})
            except Exception:
                logger.exception("Canvas browser request failed")
                self._send(500, {"error": "Canvas browser request failed."})
            else:
                self._send(200, {"data": data, "link": link})
            return
        self._send(404, {"error": "Not found."})

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class CanvasDaemonServer(ThreadingHTTPServer):
    daemon: BrowserDaemon


def monitor_session(daemon: BrowserDaemon, stop_event: threading.Event) -> None:
    """Check the live browser periodically without blocking local scraper requests."""
    interval = max(60, int(os.getenv("CANVAS_REAUTH_CHECK_SECONDS", "300")))
    while not stop_event.wait(interval):
        try:
            if not daemon.health().get("authenticated"):
                daemon.auto_reauthenticate()
        except Exception:
            logger.exception("Canvas session monitor failed")


def main() -> None:
    display = VirtualDisplay()
    display.start()
    daemon = BrowserDaemon()
    daemon.start()
    server = CanvasDaemonServer(("127.0.0.1", 8976), DaemonHandler)
    server.daemon = daemon
    logger.info("Canvas browser daemon listening on 127.0.0.1:8976 and beginning automatic ClassLink sign-in.")
    stop_event = threading.Event()
    initial_auth = threading.Thread(target=daemon.auto_reauthenticate, daemon=True)
    initial_auth.start()
    monitor = threading.Thread(target=monitor_session, args=(daemon, stop_event), daemon=True)
    monitor.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping Canvas browser daemon")
    finally:
        stop_event.set()
        server.server_close()
        daemon.close()
        display.close()


if __name__ == "__main__":
    main()
