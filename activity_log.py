"""
Activity Log — a running feed of everything the bot does.

Writes timestamped entries to activity_log.jsonl (one JSON object per line).
Also sends muted Telegram notifications for important events.

Usage from anywhere in the bot:
    from activity_log import log_event, log_llm_call, log_scrape, log_system

    log_event("photo_processed", {"ocr_chars": 1234, "has_homework": True})
    log_llm_call("openrouter/owl-alpha", "photo-extract", 1500, 2.3)
    log_scrape("canvas", 5, "new assignments")
    log_system("mc_server", "started", {"ram_mb": 1500})

Events are also mirrored to Telegram as muted (silent) notifications
for important events (errors, scrapes, LLM calls, system changes).
"""

import os
import json
import time
import logging
import queue
import re
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, "activity_log.jsonl")
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB max, then rotate
MAX_ENTRIES = 5000  # keep last 5000 entries after rotation

# Thread-safe write lock
_write_lock = threading.Lock()

# Telegram notification config
_TELEGRAM_TOKEN = None
_TELEGRAM_CHAT_ID = None
_TELEGRAM_QUEUE: queue.Queue[str] = queue.Queue(maxsize=100)
_telegram_worker_started = False
_telegram_worker_lock = threading.Lock()


def _init_telegram():
    """Lazily load Telegram credentials."""
    global _TELEGRAM_TOKEN, _TELEGRAM_CHAT_ID
    if _TELEGRAM_TOKEN is not None:
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BASE_DIR, ".env"))
        _TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        _TELEGRAM_CHAT_ID = os.getenv("SANEL_CHAT_ID", "8534649457")
    except Exception:
        _TELEGRAM_TOKEN = ""


# Event categories that should trigger a muted Telegram notification
_NOTIFY_CATEGORIES = {
    "error", "llm_call", "scrape", "system", "nightly",
    "verification", "digest", "alert",
}

# This schema contains metadata only.  It deliberately excludes raw messages,
# previews, error strings, document names, and other free text.
_SAFE_DETAIL_KEYS = {
    "model", "task", "duration_s", "cost_usd", "tokens_in", "tokens_out", "local",
    "source", "count",
    "subsystem", "action",
    "phase", "status",
    "sources",
    "routed_to",
    "has_question", "ocr_chars",
    "chunks", "error_type", "error_code",
}
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_./:-]{1,80}")
_REDACTED_FIELD = "[REDACTED_UNSAFE_FIELD]"
_REDACTED_VALUE = "[REDACTED_UNSAFE_VALUE]"

# Short label map for Telegram
_CATEGORY_ICONS = {
    "message": "\U0001f4ac",
    "photo": "\U0001f4f7",
    "voice": "\U0001f3a4",
    "llm_call": "\U0001f9e0",
    "scrape": "\U0001f4e5",
    "system": "\u2699\ufe0f",
    "nightly": "\U0001f319",
    "error": "\u274c",
    "digest": "\U0001f4f0",
    "verification": "\u2705",
    "alert": "\u26a0\ufe0f",
    "embed": "\U0001f9e0",
    "mc_server": "\u26cf\ufe0f",
    "guide_built": "\U0001f4da",
}


def _rotate_if_needed():
    """Trim the log file if it exceeds MAX_LOG_SIZE."""
    if not os.path.exists(LOG_PATH):
        return
    try:
        if os.path.getsize(LOG_PATH) < MAX_LOG_SIZE:
            return
        # Read all lines, keep last MAX_ENTRIES
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        if len(lines) > MAX_ENTRIES:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                for line in lines[-MAX_ENTRIES:]:
                    f.write(line)
    except Exception:
        pass


def _deliver_telegram_notification(text: str) -> None:
    """Deliver one already-redacted notification in the queue worker."""
    _init_telegram()
    if not _TELEGRAM_TOKEN or not _TELEGRAM_CHAT_ID:
        return

    # Scrub PII from notification text before sending to cloud
    from utils import scrub_pii
    safe_text = scrub_pii(text, aggressive=True)
    try:
        import httpx
        httpx.post(
            f"https://api.telegram.org/bot{_TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": _TELEGRAM_CHAT_ID,
                "text": safe_text,
                "parse_mode": "Markdown",
                "disable_notification": True,  # MUTED!
            },
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0),
        )
    except Exception:
        pass  # notifications are best-effort


def _telegram_notification_worker() -> None:
    """Perform best-effort Telegram I/O without blocking bot handlers."""
    while True:
        text = _TELEGRAM_QUEUE.get()
        try:
            _deliver_telegram_notification(text)
        finally:
            _TELEGRAM_QUEUE.task_done()


def _enqueue_telegram_notification(text: str) -> None:
    """Queue a notification without allowing a slow Telegram API to block work."""
    global _telegram_worker_started
    with _telegram_worker_lock:
        if not _telegram_worker_started:
            worker = threading.Thread(
                target=_telegram_notification_worker,
                name="activity-log-telegram",
                daemon=True,
            )
            worker.start()
            _telegram_worker_started = True
    try:
        _TELEGRAM_QUEUE.put_nowait(text)
    except queue.Full:
        logger.warning("Activity-log Telegram queue is full; dropping notification")


def _sanitize_detail(key: str, value) -> str | int | float | bool:
    """Keep only bounded metadata values that cannot contain natural-language PII."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
        return value
    return _REDACTED_VALUE


def _sanitize_details(details: dict | None) -> dict:
    """Normalize newly supplied details and legacy on-disk events alike."""
    if not isinstance(details, dict):
        return {}
    return {
        key: _sanitize_detail(key, value) if key in _SAFE_DETAIL_KEYS else _REDACTED_FIELD
        for key, value in details.items()
    }


def log_event(category: str, details: dict = None, notify: bool = None):
    """Log an activity event.

    Args:
        category: What happened (e.g. "message", "photo", "llm_call", "scrape", "error")
        details: Optional dict with event-specific data
        notify: Force notify (True) or suppress (False). Default: auto based on category.
    """
    # Enforce a metadata-only schema for every persisted or cloud-bound event.
    safe_details = _sanitize_details(details)

    entry = {
        "ts": time.time(),
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "cat": category,
        "details": safe_details,
    }

    with _write_lock:
        _rotate_if_needed()
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            os.chmod(LOG_PATH, 0o600)
        except Exception:
            pass

    # Telegram notification
    should_notify = notify if notify is not None else (category in _NOTIFY_CATEGORIES)
    if should_notify:
        icon = _CATEGORY_ICONS.get(category, "\U0001f4e5")
        # Build short summary
        summary = _format_event_short(icon, entry)
        _enqueue_telegram_notification(summary)


def _format_event_short(icon: str, entry: dict) -> str:
    """Format an event for a compact Telegram notification."""
    d = entry.get("details", {})
    cat = entry.get("cat", "?")
    t = entry.get("time", "?")

    safe_details = {
        key: value if value not in {_REDACTED_FIELD, _REDACTED_VALUE} else "[REDACTED]"
        for key, value in d.items()
    }

    # Category-specific formatting (use safe_details only)
    if cat == "llm_call":
        model = safe_details.get("model", "?")
        task = safe_details.get("task", "")
        dur = safe_details.get("duration_s", "?")
        try:
            cost = float(safe_details.get("cost_usd", 0))
        except ValueError:
            cost = 0.0
        return f"{icon} `{t}` `{model}` {task} ({dur}s, ${cost:.4f})"

    if cat == "scrape":
        source = safe_details.get("source", "?")
        count = safe_details.get("count", "?")
        return f"{icon} `{t}` Scraped {source}: {count}"

    if cat == "system":
        subsystem = safe_details.get("subsystem", "?")
        action = safe_details.get("action", "?")
        return f"{icon} `{t}` {subsystem}: {action}"

    if cat == "error":
        msg = safe_details.get("error_type", safe_details.get("error_code", "unknown_error"))
        source = safe_details.get("source", "")
        return f"{icon} `{t}` {source}: {msg}" if source else f"{icon} `{t}` {msg}"

    if cat == "nightly":
        phase = safe_details.get("phase", "?")
        status = safe_details.get("status", "?")
        return f"{icon} `{t}` Nightly: {phase} {status}"

    if cat == "digest":
        sources = safe_details.get("sources", "?")
        action = safe_details.get("action", "sent")
        return f"{icon} `{t}` Digest {action} ({sources} sources)"

    # Generic
    detail_str = ""
    if safe_details:
        detail_str = " " + " ".join(f"{k}={v}" for k, v in list(safe_details.items())[:3])
    return f"{icon} `{t}` {cat}{detail_str}"


def log_llm_call(model: str, task: str, duration_s: float, cost_usd: float = 0,
                 tokens_in: int = 0, tokens_out: int = 0, is_local: bool = False):
    """Convenience: log an LLM call."""
    log_event("llm_call", {
        "model": model,
        "task": task,
        "duration_s": round(duration_s, 1),
        "cost_usd": round(cost_usd, 6),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "local": is_local,
    })


def log_scrape(source: str, count: int, note: str = ""):
    """Convenience: log a scrape event."""
    # ``note`` can contain scraped or user-provided text; intentionally omit it.
    log_event("scrape", {"source": source, "count": count})


def log_system(subsystem: str, action: str, details: dict = None):
    """Convenience: log a system event (MC server start/stop, etc)."""
    d = {"subsystem": subsystem, "action": action}
    if details:
        d.update(details)
    log_event("system", d)


def log_nightly(phase: str, status: str, details: dict = None):
    """Convenience: log a nightly pipeline event."""
    d = {"phase": phase, "status": status}
    if details:
        d.update(details)
    log_event("nightly", d, notify=True)


def get_recent_events(n: int = 30, category: str = None) -> list[dict]:
    """Read the last N events from the activity log, optionally filtered by category."""
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return []

    events = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if category and entry.get("cat") != category:
                continue
            entry["details"] = _sanitize_details(entry.get("details"))
            events.append(entry)
            if len(events) >= n:
                break
        except Exception:
            continue

    return list(reversed(events))  # newest last (chronological order)


def format_events(events: list[dict]) -> str:
    """Format events for display in Telegram."""
    if not events:
        return "No events logged yet."

    lines = []
    for e in events:
        cat = e.get("cat", "?")
        icon = _CATEGORY_ICONS.get(cat, "\U0001f4e5")
        t = e.get("time", "?")
        d = _sanitize_details(e.get("details"))

        if cat == "llm_call":
            model = d.get("model", "?")
            task = d.get("task", "")
            dur = d.get("duration_s", "?")
            local = "LOCAL" if d.get("local") else "cloud"
            lines.append(f"`{t}` {icon} `{model}` {task} ({dur}s, {local})")
        elif cat == "scrape":
            source = d.get("source", "?")
            count = d.get("count", "?")
            lines.append(f"`{t}` {icon} {source}: {count}")
        elif cat == "system":
            subsystem = d.get("subsystem", "?")
            action = d.get("action", "?")
            lines.append(f"`{t}` {icon} {subsystem}: {action}")
        elif cat == "error":
            msg = d.get("error_type", d.get("error_code", "unknown_error"))
            source = d.get("source", "")
            lines.append(f"`{t}` {icon} {source}: {msg}" if source else f"`{t}` {icon} {msg}")
        elif cat == "nightly":
            phase = d.get("phase", "?")
            status = d.get("status", "?")
            lines.append(f"`{t}` {icon} {phase}: {status}")
        elif cat == "message":
            routed = d.get("routed_to", "?")
            lines.append(f"`{t}` {icon} message -> {routed}")
        elif cat == "photo":
            has_q = d.get("has_question", False)
            chars = d.get("ocr_chars", 0)
            lines.append(f"`{t}` {icon} photo (OCR: {chars} chars, question: {has_q})")
        elif cat == "digest":
            action = d.get("action", "sent")
            sources = d.get("sources", "?")
            lines.append(f"`{t}` {icon} digest {action} ({sources} sources)")
        elif cat == "embed":
            action = d.get("action", "?")
            chunks = d.get("chunks", "?")
            lines.append(f"`{t}` {icon} embed: {action} ({chunks} chunks)")
        else:
            detail_str = " ".join(f"{k}={v}" for k, v in list(d.items())[:3]) if d else ""
            lines.append(f"`{t}` {icon} {cat} {detail_str}".strip())

    return "\n".join(lines)
