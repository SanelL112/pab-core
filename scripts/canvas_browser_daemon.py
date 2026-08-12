#!/usr/bin/env python3
"""Keep one VNC-visible Firefox session alive for Canvas ClassLink access.

Run this with ``DISPLAY=:1`` after starting VNC.  Sign into ClassLink manually
in the Firefox window; the normal scraper then sends read-only requests to this
process over a localhost-only HTTP endpoint.  Passwords, cookies, and browser
storage never leave Firefox.
"""

from __future__ import annotations

import base64
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
from config import get_setting  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("canvas_browser_daemon")

CLASSLINK_APP_URL = "https://myapps.classlink.com/home"
CLASSLINK_HOSTS = {"launchpad.classlink.com", "myapps.classlink.com"}
CLASSLINK_NON_APP_LABELS = {
    "add apps", "add & share apps", "edit mode", "help", "home", "log out",
    "logout", "my apps", "notifications", "profile", "search", "settings",
    "sign out", "switch account",
}


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
        display = get_setting("CANVAS_VIRTUAL_DISPLAY", ":99")
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
        self.reauth_cooldown = int(get_setting("CANVAS_REAUTH_COOLDOWN_SECONDS", "900"))

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

    def _select_classlink_tab(self) -> bool:
        """Select a ClassLink tab, opening only the app dashboard if needed."""
        assert self.client.driver is not None
        driver = self.client.driver
        original = driver.current_window_handle
        for handle in driver.window_handles:
            driver.switch_to.window(handle)
            if urlsplit(driver.current_url).netloc in CLASSLINK_HOSTS:
                return True
        # Canvas may have opened in a new tab and replaced the LaunchPad tab.
        # Opening the dashboard is read-only and reuses the existing ClassLink
        # session; no app itself is opened.
        try:
            driver.switch_to.new_window("tab")
            driver.get(CLASSLINK_APP_URL)
            return urlsplit(driver.current_url).netloc in CLASSLINK_HOSTS
        except Exception:
            try:
                driver.switch_to.window(original)
            except Exception:
                pass
            return False

    @staticmethod
    def _clean_app_entries(entries: object) -> list[dict[str, str | None]]:
        """Remove dashboard chrome and de-duplicate visible app labels."""
        if not isinstance(entries, list):
            return []
        apps: list[dict[str, str | None]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = " ".join(str(entry.get("name") or "").split())
            if not name or len(name) > 140 or name.lower() in CLASSLINK_NON_APP_LABELS:
                continue
            # Dashboard navigation controls normally have no app-like parent
            # class and no destination. Keep actual applications even when
            # ClassLink launches them with JavaScript instead of an href.
            href = entry.get("href")
            class_name = str(entry.get("className") or "").lower()
            if not href and "app" not in class_name and "tile" not in class_name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            apps.append({"name": name, "url": str(href) if href else None})
        return apps

    def list_classlink_apps(self) -> dict[str, object]:
        """List visible ClassLink app tiles without opening any application."""
        assert self.client.driver is not None
        with self.lock:
            if not self._select_classlink_tab():
                raise CanvasSignInRequired("ClassLink is not available in the persistent Firefox session.")
            driver = self.client.driver
            deadline = time.monotonic() + 10
            entries: object = []
            while time.monotonic() < deadline:
                entries = driver.execute_script(
                    """
                    return Array.from(document.querySelectorAll(
                      'a, button, [role="link"], [role="button"]'
                    )).map((element) => ({
                      name: element.getAttribute('aria-label') || element.getAttribute('title') || element.innerText || '',
                      href: element.href || element.getAttribute('data-url') || null,
                      className: [
                        element.className || '',
                        element.parentElement?.className || '',
                        element.closest('[class*="app" i], [class*="tile" i]')?.className || ''
                      ].join(' ')
                    }));
                    """
                )
                apps = self._clean_app_entries(entries)
                if apps:
                    return {"apps": apps, "location": self.location()}
                time.sleep(0.5)
            return {"apps": [], "location": self.location()}

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
        if not get_setting("CLASSLINK_USERNAME") or not get_setting("CLASSLINK_PASSWORD"):
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

    def download_canvas_file(self, file_id: str) -> tuple[bytes, str, str]:
        """Download a bounded Canvas file through Firefox, never via copied cookies."""
        if not file_id.isdigit():
            raise CanvasSessionError("Canvas file IDs must be numeric.")
        max_bytes = max(1, int(get_setting("CANVAS_STUDY_FILE_MAX_MB", "15"))) * 1024 * 1024
        with self.lock:
            if not self._select_canvas_tab():
                raise CanvasSignInRequired("Canvas is not open in the persistent Firefox session.")
            metadata = self.client.get_json(f"/api/v1/files/{file_id}")
            if not isinstance(metadata, dict) or not metadata.get("url"):
                raise CanvasSessionError("Canvas did not provide a downloadable file URL.")
            result = self.client.driver.execute_async_script(
                """
                const target = arguments[0];
                const maxBytes = arguments[1];
                const done = arguments[arguments.length - 1];
                fetch(target, {credentials: 'include', redirect: 'follow'}).then(async (response) => {
                  const declaredSize = Number(response.headers.get('content-length') || 0);
                  if (!response.ok) throw new Error(`HTTP ${response.status}`);
                  if (declaredSize && declaredSize > maxBytes) throw new Error('File is larger than the configured limit.');
                  const buffer = await response.arrayBuffer();
                  if (buffer.byteLength > maxBytes) throw new Error('File is larger than the configured limit.');
                  const bytes = new Uint8Array(buffer);
                  let binary = '';
                  const chunkSize = 0x8000;
                  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
                    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
                  }
                  done({
                    data: btoa(binary),
                    contentType: response.headers.get('content-type') || 'application/octet-stream'
                  });
                }).catch((error) => done({error: String(error)}));
                """,
                metadata["url"],
                max_bytes,
            )
            if not isinstance(result, dict) or result.get("error") or not result.get("data"):
                raise CanvasSessionError(
                    f"Canvas file download failed: {result.get('error', 'unknown error') if isinstance(result, dict) else 'unknown error'}"
                )
            try:
                content = base64.b64decode(result["data"], validate=True)
            except (TypeError, ValueError) as exc:
                raise CanvasSessionError("Canvas returned invalid file data.") from exc
            if len(content) > max_bytes:
                raise CanvasSessionError("Canvas file is larger than the configured limit.")
            filename = str(metadata.get("display_name") or metadata.get("filename") or f"canvas-{file_id}")
            return content, str(result.get("contentType") or "application/octet-stream"), filename

    def close(self) -> None:
        self.client.close()


class DaemonHandler(BaseHTTPRequestHandler):
    server: "CanvasDaemonServer"

    def do_GET(self) -> None:  # noqa: N802
        route = urlsplit(self.path)
        if route.path == "/health":
            self._send(200, self.server.daemon.health())
            return
        if route.path == "/apps":
            try:
                self._send(200, self.server.daemon.list_classlink_apps())
            except CanvasSignInRequired as exc:
                self._send(401, {"error": str(exc)})
            except Exception:
                logger.exception("ClassLink app listing failed")
                self._send(500, {"error": "Could not list ClassLink apps."})
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
        if route.path == "/download":
            file_id = parse_qs(route.query).get("file_id", [""])[0]
            try:
                content, content_type, filename = self.server.daemon.download_canvas_file(file_id)
            except CanvasSignInRequired as exc:
                self._send(401, {"error": str(exc)})
            except CanvasSessionError as exc:
                self._send(400, {"error": str(exc)})
            except Exception:
                logger.exception("Canvas file download failed")
                self._send(500, {"error": "Canvas file download failed."})
            else:
                self._send_binary(200, content, content_type, filename)
            return
        self._send(404, {"error": "Not found."})

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_binary(self, status: int, body: bytes, content_type: str, filename: str) -> None:
        safe_filename = filename.replace('"', "'").replace("\r", "").replace("\n", "")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{safe_filename}"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class CanvasDaemonServer(ThreadingHTTPServer):
    daemon: BrowserDaemon


def monitor_session(daemon: BrowserDaemon, stop_event: threading.Event) -> None:
    """Check the live browser periodically without blocking local scraper requests."""
    interval = max(60, int(get_setting("CANVAS_REAUTH_CHECK_SECONDS", "300")))
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
