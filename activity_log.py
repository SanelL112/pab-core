"""Structured, privacy-preserving activity diagnostics.

Disk writes are bounded and serialized across processes.  Optional Telegram
alerts are delivered by a bounded background queue, so an unavailable network
cannot stall bot handlers or batch jobs.
"""
from __future__ import annotations

import atexit
from collections import deque
from datetime import datetime, timezone
import fcntl
import json
import logging
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import config


logger = logging.getLogger(__name__)

# Keep this public compatibility name for diagnostic tools and tests.  Its
# default is now the configured private log root instead of the checkout.
BASE_DIR = str(config.LOG_DIR)
LOG_PATH = str(config.ACTIVITY_LOG_FILE)
MAX_LOG_SIZE = 5 * 1024 * 1024
MAX_ENTRIES = 5_000
MAX_DETAIL_ITEMS = 40
MAX_COLLECTION_ITEMS = 20
MAX_TEXT_LENGTH = 500

_write_lock = threading.RLock()
_notify_queue: queue.Queue[str | None] = queue.Queue(maxsize=100)
_notify_thread: threading.Thread | None = None
_notify_thread_lock = threading.Lock()
_notify_stop = threading.Event()
_dropped_notifications = 0


_NOTIFY_CATEGORIES = {
    "error", "scrape", "system", "nightly", "verification", "digest", "alert",
}

_CATEGORY_ICONS = {
    "message": "💬", "photo": "📷", "voice": "🎤", "llm_call": "🧠",
    "scrape": "📥", "system": "⚙️", "nightly": "🌙", "error": "❌",
    "digest": "📰", "verification": "✅", "alert": "⚠️", "embed": "🧠",
    "mc_server": "⛏️", "guide_built": "📚",
}

_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|bot[_-]?token)",
    re.IGNORECASE,
)
_CONTENT_KEY = re.compile(
    r"(?:^|_)(?:body|content|prompt|raw|transcript|ocr_text|user_text|response)(?:$|_)",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[-. (]*)?(?:\d{3}[-. )]*)\d{3}[-. ]*\d{4}(?!\d)")
_BOT_TOKEN_URL = re.compile(r"/bot[^/\s]+/", re.IGNORECASE)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key|authorization)\b\s*[:=]\s*[^\s,;]+"
)
_HOME_PATH = re.compile(r"/(?:home|root)/[^/\s]+(?:/[^\s]*)?")


def _redacted_length(value: object) -> str:
    try:
        size = len(value)  # type: ignore[arg-type]
    except (TypeError, AttributeError):
        size = len(str(value))
    return f"[redacted:{size}]"


def _safe_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or "private-host"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (ValueError, TypeError):
        return "[redacted-url]"


_URL = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def _sanitize_text(value: object, *, limit: int = MAX_TEXT_LENGTH) -> str:
    text = str(value)
    text = _BOT_TOKEN_URL.sub("/bot[redacted]/", text)
    text = _KEY_VALUE_SECRET.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _EMAIL.sub("[redacted-email]", text)
    text = _PHONE.sub("[redacted-phone]", text)
    text = _URL.sub(_safe_url, text)
    text = _HOME_PATH.sub("[private-path]", text)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Recursively bound and redact an event value before it reaches disk."""
    if _SECRET_KEY.search(key):
        return "[redacted]"
    if _CONTENT_KEY.search(key):
        return _redacted_length(value)
    if depth >= 4:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= MAX_DETAIL_ITEMS:
                result["_truncated"] = len(value) - MAX_DETAIL_ITEMS
                break
            safe_key = _sanitize_text(item_key, limit=80)
            result[safe_key] = _sanitize(item_value, key=safe_key, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [_sanitize(item, key=key, depth=depth + 1) for item in items[:MAX_COLLECTION_ITEMS]]
        if len(items) > MAX_COLLECTION_ITEMS:
            result.append(f"[truncated:{len(items) - MAX_COLLECTION_ITEMS}]")
        return result
    return _sanitize_text(repr(value))


def _path() -> Path:
    return Path(LOG_PATH)


def _atomic_replace(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary_path.unlink()
        except OSError:
            pass
        raise


def _rotate_if_needed(path: Path | None = None) -> None:
    path = path or _path()
    if not path.exists() or path.stat().st_size < MAX_LOG_SIZE:
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        lines = deque(handle, maxlen=MAX_ENTRIES)
    _atomic_replace(path, "".join(lines).encode("utf-8"))


def _append_entry(entry: dict[str, Any]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    lock_path = path.with_name(f".{path.name}.lock")
    payload = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with _write_lock:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            _rotate_if_needed(path)
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)
                os.write(fd, payload)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _notification_worker() -> None:
    """Deliver alerts with pooling and a ten-per-minute rate limit."""
    import httpx

    sent: deque[float] = deque()
    timeout = httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0)
    limits = httpx.Limits(max_connections=2, max_keepalive_connections=1)
    with httpx.Client(timeout=timeout, limits=limits) as client:
        while not _notify_stop.is_set():
            try:
                text = _notify_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if text is None:
                    return
                now = time.monotonic()
                while sent and now - sent[0] >= 60:
                    sent.popleft()
                if len(sent) >= 10:
                    continue
                response = client.post(
                    f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": config.TELEGRAM_CHAT_ID,
                        "text": text[:3500],
                        "disable_notification": True,
                    },
                )
                response.raise_for_status()
                sent.append(now)
            except Exception as exc:
                # Never include the token-bearing request URL or response body.
                logger.warning("Activity notification delivery failed (%s)", type(exc).__name__)
            finally:
                _notify_queue.task_done()


def _ensure_notification_worker() -> bool:
    global _notify_thread
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    with _notify_thread_lock:
        if _notify_thread is None or not _notify_thread.is_alive():
            _notify_stop.clear()
            _notify_thread = threading.Thread(
                target=_notification_worker,
                name="activity-notifier",
                daemon=True,
            )
            _notify_thread.start()
    return True


def _send_telegram_notification(text: str) -> None:
    """Queue a scrubbed notification without blocking the caller."""
    global _dropped_notifications
    if not _ensure_notification_worker():
        return
    try:
        _notify_queue.put_nowait(_sanitize_text(text, limit=3500))
    except queue.Full:
        _dropped_notifications += 1
        logger.warning("Activity notification queue full; dropped=%d", _dropped_notifications)


def close_activity_log(timeout: float = 2.0) -> None:
    """Best-effort notifier shutdown for application lifecycle hooks."""
    thread = _notify_thread
    if thread is None or not thread.is_alive():
        return
    _notify_stop.set()
    try:
        _notify_queue.put_nowait(None)
    except queue.Full:
        pass
    thread.join(timeout=max(0.0, timeout))


atexit.register(close_activity_log)


def log_event(category: str, details: dict | None = None, notify: bool | None = None) -> None:
    """Persist a bounded event and optionally queue a muted owner alert."""
    safe_category = _sanitize_text(category, limit=64) or "unknown"
    now = datetime.now(timezone.utc)
    entry = {
        "ts": now.timestamp(),
        "timestamp": now.isoformat().replace("+00:00", "Z"),
        "time": now.astimezone().strftime("%H:%M:%S"),
        "date": now.astimezone().strftime("%Y-%m-%d"),
        "cat": safe_category,
        "details": _sanitize(details or {}),
    }
    try:
        _append_entry(entry)
    except Exception as exc:
        logger.error("Unable to persist activity event (%s)", type(exc).__name__)

    should_notify = notify if notify is not None else safe_category in _NOTIFY_CATEGORIES
    if should_notify:
        _send_telegram_notification(_format_event_short(_CATEGORY_ICONS.get(safe_category, "📥"), entry))


def _format_event_short(icon: str, entry: dict) -> str:
    d = _sanitize(entry.get("details", {}))
    cat = _sanitize_text(entry.get("cat", "?"), limit=64)
    t = _sanitize_text(entry.get("time", "?"), limit=16)
    if not isinstance(d, dict):
        d = {}
    if cat == "llm_call":
        return f"{icon} {t} {d.get('model', '?')} {d.get('task', '')} ({d.get('duration_s', '?')}s)"
    if cat == "scrape":
        return f"{icon} {t} scrape {d.get('source', '?')}: {d.get('count', '?')}"
    if cat == "system":
        return f"{icon} {t} {d.get('subsystem', '?')}: {d.get('action', '?')}"
    if cat == "error":
        return f"{icon} {t} {d.get('source', 'application')}: {d.get('message', 'error')}"
    if cat == "nightly":
        return f"{icon} {t} nightly {d.get('phase', '?')}: {d.get('status', '?')}"
    if cat == "digest":
        return f"{icon} {t} digest {d.get('action', 'sent')}"
    return f"{icon} {t} {cat}"


def log_llm_call(model: str, task: str, duration_s: float, cost_usd: float = 0,
                 tokens_in: int = 0, tokens_out: int = 0, is_local: bool = False) -> None:
    log_event("llm_call", {
        "model": model, "task": task, "duration_s": round(duration_s, 1),
        "cost_usd": round(cost_usd, 6), "tokens_in": tokens_in,
        "tokens_out": tokens_out, "local": is_local,
    })


def log_scrape(source: str, count: int, note: str = "") -> None:
    log_event("scrape", {"source": source, "count": count, "note": note})


def log_system(subsystem: str, action: str, details: dict | None = None) -> None:
    payload = {"subsystem": subsystem, "action": action}
    if details:
        payload.update(details)
    log_event("system", payload)


def log_nightly(phase: str, status: str, details: dict | None = None) -> None:
    payload = {"phase": phase, "status": status}
    if details:
        payload.update(details)
    log_event("nightly", payload, notify=True)


def get_recent_events(n: int = 30, category: str | None = None) -> list[dict]:
    """Return at most ``n`` recent valid entries without loading the whole log."""
    if n <= 0:
        return []
    path = _path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            candidates = deque(handle, maxlen=min(max(n * 5, n), MAX_ENTRIES))
    except OSError:
        return []
    events: list[dict] = []
    for line in reversed(candidates):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if category and entry.get("cat") != category:
            continue
        events.append(_sanitize(entry))
        if len(events) >= n:
            break
    return list(reversed(events))


def format_events(events: list[dict]) -> str:
    """Format stored events as bounded plain text for Telegram."""
    if not events:
        return "No events logged yet."
    lines: list[str] = []
    for raw in events[:100]:
        entry = _sanitize(raw)
        if not isinstance(entry, dict):
            continue
        icon = _CATEGORY_ICONS.get(str(entry.get("cat")), "📥")
        lines.append(_format_event_short(icon, entry))
    return "\n".join(lines) or "No valid events logged yet."
