"""Synchronize school assignments to private CalDAV and Google calendars.

The local CalDAV calendar is canonical. Google is a one-way mirror accessed
through the existing Composio connection. This module never writes externally
until its local enabled flag is set by an authenticated Telegram action.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import difflib
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

import config

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo(config.ASSIGNMENT_CALENDAR_TIMEZONE)
OWNER_MARKER = "X-PAB-OWNER:assignment-calendar"


class CalendarSyncError(RuntimeError):
    """Raised for a calendar configuration or transport failure."""


@dataclass(frozen=True)
class Assignment:
    source: str
    external_id: str
    title: str
    course: str
    due_at: str | None = None
    due_date: str | None = None
    url: str | None = None
    task_type: str = "Assignment"
    status: str = "Not started"
    official: bool = False

    @property
    def key(self) -> str:
        return f"{self.source}:{self.external_id}"

    @property
    def completed(self) -> bool:
        return self.status.lower() in {"done", "completed", "submitted", "graded"}

    @property
    def is_date_only(self) -> bool:
        return not self.due_at and bool(self.due_date)

    def local_due(self) -> datetime | date:
        if self.due_at:
            parsed = datetime.fromisoformat(self.due_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=LOCAL_TZ)
            return parsed.astimezone(LOCAL_TZ)
        if self.due_date:
            return date.fromisoformat(self.due_date[:10])
        raise CalendarSyncError(f"Assignment {self.key} has no due date.")

    def fingerprint(self) -> str:
        payload = {
            "version": 2,
            "title": self.title,
            "course": self.course,
            "due_at": self.due_at,
            "due_date": self.due_date,
            "url": self.url,
            "task_type": self.task_type,
            "status": self.status,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def event_uid(self) -> str:
        digest = hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:32]
        return f"pab-{digest}@assignment-calendar"


@dataclass(frozen=True)
class SyncAction:
    kind: str
    assignment: Assignment
    caldav_uid: str | None = None
    google_event_id: str | None = None


def _escape_ics(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ical_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def render_ical_event(assignment: Assignment) -> str:
    """Render a bot-owned VEVENT with the selected local reminder behavior."""
    due = assignment.local_due()
    title = assignment.title if not assignment.completed else f"Completed: {assignment.title}"
    description_lines = [
        "Managed by Personal Assistant Bot.",
        f"Source: {assignment.source}",
        f"Course: {assignment.course}",
        f"Source ID: {assignment.key}",
    ]
    if assignment.url:
        description_lines.append(f"Open assignment: {assignment.url}")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Personal Assistant Bot//Assignment Calendar//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{assignment.event_uid()}",
        f"DTSTAMP:{_ical_datetime(datetime.now(timezone.utc))}",
        f"SUMMARY:{_escape_ics(title)}",
        f"DESCRIPTION:{_escape_ics(chr(10).join(description_lines))}",
        OWNER_MARKER,
        f"X-PAB-SOURCE:{_escape_ics(assignment.key)}",
        "STATUS:CONFIRMED",
        f"CATEGORIES:{_escape_ics(assignment.course)}",
    ]
    if assignment.url:
        lines.append(f"URL:{assignment.url}")

    if isinstance(due, date) and not isinstance(due, datetime):
        end_day = due + timedelta(days=1)
        lines.extend([
            f"DTSTART;VALUE=DATE:{due.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end_day.strftime('%Y%m%d')}",
        ])
        if not assignment.completed:
            reminder = datetime.combine(due, time(7, 0), tzinfo=LOCAL_TZ)
            lines.extend([
                "BEGIN:VALARM",
                f"TRIGGER;VALUE=DATE-TIME:{_ical_datetime(reminder)}",
                "ACTION:DISPLAY",
                "DESCRIPTION:Assignment due today",
                "END:VALARM",
            ])
    else:
        assert isinstance(due, datetime)
        lines.extend([
            f"DTSTART:{_ical_datetime(due)}",
            f"DTEND:{_ical_datetime(due + timedelta(minutes=15))}",
        ])
        if not assignment.completed:
            lines.extend([
                "BEGIN:VALARM",
                "TRIGGER:-P1D",
                "ACTION:DISPLAY",
                "DESCRIPTION:Assignment due tomorrow",
                "END:VALARM",
                "BEGIN:VALARM",
                "TRIGGER:-PT1H",
                "ACTION:DISPLAY",
                "DESCRIPTION:Assignment due in one hour",
                "END:VALARM",
            ])

    lines.extend(["END:VEVENT", "END:VCALENDAR", ""])
    return "\r\n".join(lines)


class CalDavCalendar:
    """Small WebDAV client for a single Radicale calendar collection."""

    def __init__(self, collection_url: str, username: str, password: str, session: requests.Session | None = None) -> None:
        self.collection_url = collection_url.rstrip("/") + "/" if collection_url else ""
        self.username = username
        self.password = password
        self.session = session or requests.Session()

    def configured(self) -> bool:
        return bool(self.collection_url and self.username and self.password)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            response = self.session.request(
                method,
                url,
                auth=(self.username, self.password),
                timeout=20,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise CalendarSyncError(f"CalDAV request failed: {exc}") from exc
        return response

    def ensure_collection(self) -> None:
        if not self.configured():
            raise CalendarSyncError("CalDAV URL and credentials are not configured.")
        response = self._request(
            "MKCALENDAR",
            self.collection_url,
            headers={"Content-Type": "application/xml; charset=utf-8"},
        )
        # Radicale returns 409 when MKCALENDAR targets an existing collection.
        # Confirm that the collection is actually accessible before treating it
        # as an idempotent success; a 409 for a missing parent remains an error.
        if response.status_code == 409:
            existing = self._request("PROPFIND", self.collection_url, headers={"Depth": "0"})
            if existing.status_code in {200, 207}:
                return
        if response.status_code not in {200, 201, 204, 405}:
            raise CalendarSyncError(f"Could not create or access CalDAV calendar ({response.status_code}).")

    def upsert(self, assignment: Assignment) -> str:
        self.ensure_collection()
        uid = assignment.event_uid()
        url = self.collection_url + quote(uid + ".ics", safe="")
        response = self._request(
            "PUT",
            url,
            data=render_ical_event(assignment).encode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        if response.status_code not in {200, 201, 204}:
            raise CalendarSyncError(f"Could not save CalDAV event ({response.status_code}).")
        return uid

    def delete(self, uid: str) -> None:
        url = self.collection_url + quote(uid + ".ics", safe="")
        response = self._request("DELETE", url)
        if response.status_code not in {200, 202, 204, 404}:
            raise CalendarSyncError(f"Could not delete CalDAV event ({response.status_code}).")


class GoogleCalendarMirror:
    """One-way Google mirror through configured Composio actions."""

    def __init__(
        self,
        *,
        calendar_id: str = "",
        calendar_name: str = "Assignments",
        calendar_create_tool: str = "",
        create_tool: str = "",
        update_tool: str = "",
        delete_tool: str = "",
    ) -> None:
        self.calendar_id = calendar_id
        self.calendar_name = calendar_name
        self.calendar_create_tool = calendar_create_tool
        self.create_tool = create_tool
        self.update_tool = update_tool
        self.delete_tool = delete_tool

    @classmethod
    def from_config(cls, calendar_id: str | None = None) -> "GoogleCalendarMirror":
        return cls(
            calendar_id=config.ASSIGNMENT_GOOGLE_CALENDAR_ID if calendar_id is None else calendar_id,
            calendar_name=config.ASSIGNMENT_GOOGLE_CALENDAR_NAME,
            calendar_create_tool=config.ASSIGNMENT_GOOGLE_CALENDAR_CREATE_TOOL,
            create_tool=config.ASSIGNMENT_GOOGLE_CREATE_TOOL,
            update_tool=config.ASSIGNMENT_GOOGLE_UPDATE_TOOL,
            delete_tool=config.ASSIGNMENT_GOOGLE_DELETE_TOOL,
        )

    def event_actions_configured(self) -> bool:
        return bool(self.create_tool and self.update_tool and self.delete_tool)

    def configured(self) -> bool:
        return bool(self.calendar_id and self.event_actions_configured())

    def can_create_calendar(self) -> bool:
        return bool(self.calendar_create_tool and self.event_actions_configured())

    def _call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if not tool:
            raise CalendarSyncError("A required Composio Calendar action slug is not configured.")
        from scrapers.composio_fetcher import _call_mcp

        response = _call_mcp(tool, arguments)
        if not response or not response.get("successful"):
            detail = (response or {}).get("data", {}).get("message", "no response")
            raise CalendarSyncError(f"Composio Calendar action failed: {detail}")
        return response.get("data", {}).get("response_data", response.get("data", {}))

    @staticmethod
    def _response_id(response: Any) -> str | None:
        if isinstance(response, dict):
            event_id = response.get("id")
            if event_id:
                return str(event_id)
            for key in ("calendar", "event"):
                nested = response.get(key)
                if isinstance(nested, dict) and nested.get("id"):
                    return str(nested["id"])
        if isinstance(response, list):
            for item in response:
                event_id = GoogleCalendarMirror._response_id(item)
                if event_id:
                    return event_id
        return None

    def ensure_calendar(self) -> str:
        """Create the dedicated calendar only during an explicit enable action."""
        if self.calendar_id:
            return self.calendar_id
        if not self.can_create_calendar():
            raise CalendarSyncError("Google Calendar ID or verified calendar-creation action is required.")
        calendar_id = self._response_id(self._call(self.calendar_create_tool, {"summary": self.calendar_name}))
        if not calendar_id:
            raise CalendarSyncError("Composio did not return a Google Calendar ID for the dedicated calendar.")
        self.calendar_id = calendar_id
        return calendar_id

    @staticmethod
    def _payload(assignment: Assignment) -> dict[str, Any]:
        due = assignment.local_due()
        title = assignment.title if not assignment.completed else f"Completed: {assignment.title}"
        description = "\n".join([
            "Managed by Personal Assistant Bot.",
            "PAB-OWNER: assignment-calendar",
            f"PAB-SOURCE: {assignment.key}",
            f"Course: {assignment.course}",
            *( [f"Open assignment: {assignment.url}"] if assignment.url else [] ),
        ])
        payload: dict[str, Any] = {"summary": title, "description": description}
        if assignment.completed:
            # A completed event must not inherit a dedicated calendar default.
            payload["reminders"] = {"useDefault": False, "overrides": []}
        if isinstance(due, date) and not isinstance(due, datetime):
            payload["start"] = {"date": due.isoformat()}
            payload["end"] = {"date": (due + timedelta(days=1)).isoformat()}
            # Google cannot schedule an alarm after an all-day event begins.
            # Apple receives the exact 7 AM CalDAV alarm; Google keeps its
            # dedicated calendar defaults for the mirror.
            if not assignment.completed:
                payload["reminders"] = {"useDefault": True}
        else:
            assert isinstance(due, datetime)
            payload["start"] = {"dateTime": due.isoformat(), "timeZone": config.ASSIGNMENT_CALENDAR_TIMEZONE}
            payload["end"] = {"dateTime": (due + timedelta(minutes=15)).isoformat(), "timeZone": config.ASSIGNMENT_CALENDAR_TIMEZONE}
            if not assignment.completed:
                payload["reminders"] = {
                    "useDefault": False,
                    "overrides": [{"method": "popup", "minutes": 24 * 60}, {"method": "popup", "minutes": 60}],
                }
        return payload

    def upsert(self, assignment: Assignment, event_id: str | None = None) -> str:
        if not self.configured():
            raise CalendarSyncError("Google Calendar mirror is not configured with a calendar ID and Composio actions.")
        payload = self._payload(assignment)
        payload["calendar_id"] = self.calendar_id
        if event_id:
            payload["event_id"] = event_id
            response = self._call(self.update_tool, payload)
            return self._response_id(response) or event_id
        response = self._call(self.create_tool, payload)
        event_id = self._response_id(response)
        if not event_id:
            raise CalendarSyncError("Composio did not return a Google Calendar event ID.")
        return str(event_id)

    def delete(self, event_id: str) -> None:
        if not self.configured():
            raise CalendarSyncError("Google Calendar mirror is not configured.")
        self._call(self.delete_tool, {"calendar_id": self.calendar_id, "event_id": event_id})


class SyncStore:
    """Durable mapping and approval history outside the repository."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config.ASSIGNMENT_CALENDAR_DATABASE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connect().close()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    source_key TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    caldav_uid TEXT,
                    google_event_id TEXT,
                    official INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                    batch_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS approval_preferences (
                    source TEXT NOT NULL,
                    course TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    approved_count INTEGER NOT NULL DEFAULT 0,
                    rejected_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (source, course, task_type)
                );
                CREATE TABLE IF NOT EXISTS learned_patterns (
                    pattern TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    approved_weight INTEGER NOT NULL DEFAULT 0,
                    rejected_weight INTEGER NOT NULL DEFAULT 0,
                    last_updated TEXT NOT NULL
                );
                """
            )

    def is_enabled(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = 'enabled'").fetchone()
        return bool(row and row["value"] == "1")

    def set_enabled(self, enabled: bool) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('enabled', ?)", ("1" if enabled else "0",))

    def setting(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, value))

    def event(self, source_key: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute("SELECT * FROM events WHERE source_key = ?", (source_key,)).fetchone()

    def active_events(self) -> list[sqlite3.Row]:
        with self._connect() as db:
            return list(db.execute("SELECT * FROM events"))

    def save_event(self, assignment: Assignment, caldav_uid: str | None, google_event_id: str | None) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO events
                (source_key, fingerprint, caldav_uid, google_event_id, official, completed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    assignment.key,
                    assignment.fingerprint(),
                    caldav_uid,
                    google_event_id,
                    int(assignment.official),
                    int(assignment.completed),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def remove_event(self, source_key: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM events WHERE source_key = ?", (source_key,))

    def save_proposal(self, assignments: Iterable[Assignment]) -> str:
        batch_id = uuid4().hex[:12]
        payload = json.dumps([asdict(item) for item in assignments])
        with self._connect() as db:
            db.execute(
                "INSERT INTO proposals(batch_id, payload, created_at) VALUES (?, ?, ?)",
                (batch_id, payload, datetime.now(timezone.utc).isoformat()),
            )
        return batch_id

    def proposal(self, batch_id: str) -> list[Assignment]:
        with self._connect() as db:
            row = db.execute("SELECT payload, resolved_at FROM proposals WHERE batch_id = ?", (batch_id,)).fetchone()
        if row is None or row["resolved_at"]:
            raise CalendarSyncError("That calendar proposal has expired or was already resolved.")
        return [Assignment(**item) for item in json.loads(row["payload"])]

    def resolve_proposal(self, batch_id: str, approved: bool) -> None:
        assignments = self.proposal(batch_id)
        with self._connect() as db:
            now_iso = datetime.now(timezone.utc).isoformat()
            for item in assignments:
                db.execute(
                    """INSERT INTO approval_preferences(source, course, task_type, approved_count, rejected_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source, course, task_type) DO UPDATE SET
                      approved_count = approved_count + excluded.approved_count,
                      rejected_count = rejected_count + excluded.rejected_count""",
                    (item.source, item.course, item.task_type, 1 if approved else 0, 0 if approved else 1),
                )
                tokens = _extract_title_tokens(item.title)
                for tok in tokens:
                    db.execute(
                        """INSERT INTO learned_patterns(pattern, category, approved_weight, rejected_weight, last_updated)
                        VALUES (?, 'title_token', ?, ?, ?)
                        ON CONFLICT(pattern) DO UPDATE SET
                          approved_weight = approved_weight + excluded.approved_weight,
                          rejected_weight = rejected_weight + excluded.rejected_weight,
                          last_updated = excluded.last_updated""",
                        (tok, 1 if approved else 0, 0 if approved else 1, now_iso),
                    )
            db.execute("UPDATE proposals SET resolved_at = ? WHERE batch_id = ?", (now_iso, batch_id))

    def is_suppressed(self, item: Assignment) -> bool:
        if item.official:
            return False
        tokens = _extract_title_tokens(item.title)
        with self._connect() as db:
            pref = db.execute(
                "SELECT approved_count, rejected_count FROM approval_preferences WHERE source = ? AND course = ? AND task_type = ?",
                (item.source, item.course, item.task_type),
            ).fetchone()
            if pref and pref["rejected_count"] >= 3 and pref["approved_count"] == 0:
                return True

            if tokens:
                placeholders = ",".join("?" for _ in tokens)
                rows = db.execute(
                    f"SELECT pattern, approved_weight, rejected_weight FROM learned_patterns WHERE pattern IN ({placeholders})",
                    tokens,
                ).fetchall()
                for r in rows:
                    if r["rejected_weight"] >= 2 and r["rejected_weight"] > 2 * r["approved_weight"]:
                        return True
        return False

    def recommendation(self, item: Assignment) -> bool:
        if self.is_suppressed(item):
            return False
        with self._connect() as db:
            row = db.execute(
                "SELECT approved_count, rejected_count FROM approval_preferences WHERE source = ? AND course = ? AND task_type = ?",
                (item.source, item.course, item.task_type),
            ).fetchone()
            if row and row["approved_count"] > row["rejected_count"]:
                return True

            tokens = _extract_title_tokens(item.title)
            if tokens:
                placeholders = ",".join("?" for _ in tokens)
                pat_rows = db.execute(
                    f"SELECT approved_weight, rejected_weight FROM learned_patterns WHERE pattern IN ({placeholders})",
                    tokens,
                ).fetchall()
                if any(p["approved_weight"] > p["rejected_weight"] for p in pat_rows):
                    return True
        return False


def _assignment_from_payload(payload: dict[str, Any], source: str) -> Assignment | None:
    title = str(payload.get("title") or payload.get("name") or "").strip()
    due_at = payload.get("due_at") or payload.get("dueAt")
    due_date = payload.get("due_date") or payload.get("dueDate")
    if isinstance(due_date, dict):
        try:
            due_date = date(int(due_date["year"]), int(due_date["month"]), int(due_date["day"])).isoformat()
        except (KeyError, TypeError, ValueError):
            due_date = None
    if not title or not (due_at or due_date):
        return None
    external_id = str(payload.get("id") or payload.get("external_id") or "").strip()
    if not external_id:
        external_id = f"gen-{hashlib.md5(f"{source}:{title}:{due_at or due_date}".encode()).hexdigest()[:12]}"
    task_type = str(payload.get("task_type") or "Assignment")
    official_flag = payload.get("official")
    if official_flag is not None:
        is_official = bool(official_flag)
    else:
        is_official = True
    return Assignment(
        source=source,
        external_id=external_id,
        title=title,
        course=str(payload.get("course") or "Uncategorized"),
        due_at=str(due_at) if due_at else None,
        due_date=str(due_date)[:10] if due_date else None,
        url=str(payload.get("url") or payload.get("html_url") or "") or None,
        task_type=task_type if task_type in {"Assignment", "Test", "Project", "Reading", "Other"} else "Assignment",
        status=str(payload.get("status") or "Not started"),
        official=is_official,
    )


def _extract_title_tokens(title: str) -> list[str]:
    words = re.findall(r"[a-z0-9_#/-]+", title.lower())
    stop_words = {"the", "and", "a", "an", "of", "to", "in", "for", "on", "with", "at", "by", "from", "is", "or", "your", "that", "this"}
    cleaned = [w for w in words if len(w) >= 3 and w not in stop_words]
    tokens = list(cleaned)
    for i in range(len(cleaned) - 1):
        tokens.append(f"{cleaned[i]} {cleaned[i+1]}")
    return tokens


def _normalize_title_for_dedupe(title: str) -> str:
    t = re.sub(r"^\[.*?\]\s*", "", title)
    t = re.sub(r"[^\w\s]", "", t.lower())
    return re.sub(r"\s+", " ", t).strip()


def _normalize_course_for_dedupe(course: str) -> str:
    c = course.lower().strip()
    c = re.sub(r"-\s*\d+.*$", "", c)
    c = re.sub(r"[^\w\s]", "", c)
    return re.sub(r"\s+", " ", c).strip()


def _assignment_date_str(item: Assignment) -> str:
    due = item.local_due()
    if isinstance(due, datetime):
        return due.date().isoformat()
    elif isinstance(due, date):
        return due.isoformat()
    return ""


def _dates_are_close(d1_str: str, d2_str: str, max_days: int = 1) -> bool:
    try:
        d1 = date.fromisoformat(d1_str[:10])
        d2 = date.fromisoformat(d2_str[:10])
        return abs((d1 - d2).days) <= max_days
    except Exception:
        return d1_str[:10] == d2_str[:10]


def _upsert_merge_assignments(base: Assignment, incoming: Assignment) -> Assignment:
    """Deep-merge two matching assignments to enrich metadata without data loss."""
    # 1. Course: Keep more descriptive course name
    course = base.course
    if base.course in {"Canvas Coursework", "Uncategorized", "Google Docs"} and incoming.course not in {"Canvas Coursework", "Uncategorized", "Google Docs"}:
        course = incoming.course

    # 2. Title: Keep the longer / more descriptive title
    title = base.title if len(base.title) >= len(incoming.title) else incoming.title

    # 3. ID: Prefer official upstream Canvas/Classroom ID over synthesized gen- IDs
    external_id = base.external_id
    if (base.external_id.startswith("gen-") or base.external_id.startswith("ext-") or base.external_id.startswith("page-")) and not (incoming.external_id.startswith("gen-") or incoming.external_id.startswith("ext-") or incoming.external_id.startswith("page-")):
        external_id = incoming.external_id

    # 4. Due Date: If incoming has specific live pacing/agenda date and base is missing/generic, prioritize incoming
    due_at = base.due_at or incoming.due_at
    due_date = base.due_date or incoming.due_date
    if incoming.source in {"canvas_pages", "google_docs"} and incoming.due_date:
        due_date = incoming.due_date
        if incoming.due_at:
            due_at = incoming.due_at

    # 5. URL: Preserve baseline assignment link or enrich
    url = base.url or incoming.url

    # 6. Task Type: Specific types (Test/Project/Reading) override generic Assignment
    task_type = base.task_type
    if base.task_type == "Assignment" and incoming.task_type in {"Test", "Project", "Reading", "Other"}:
        task_type = incoming.task_type

    # 7. Official: If either is official, resulting merged is official
    official = base.official or incoming.official

    # 8. Status: Preserve Completed status
    status = "Completed" if (base.status == "Completed" or incoming.status == "Completed") else (base.status or incoming.status or "Not started")

    # 9. Source: Keep primary authoritative source
    source = base.source if base.source in {"canvas", "google_classroom"} else incoming.source

    return Assignment(
        source=source,
        external_id=external_id,
        title=title,
        course=course,
        due_at=due_at,
        due_date=due_date,
        url=url,
        task_type=task_type,
        status=status,
        official=official,
    )


def collect_assignments(use_composio: bool | None = None) -> list[Assignment]:
    """Collect structured sources without interpreting the human digest text."""
    from scrapers.canvas_scraper import get_calendar_assignments as canvas_assignments
    from scrapers.google_docs_calendar import get_calendar_assignments as google_docs_assignments
    from scrapers.notion_client import get_calendar_tasks

    use_composio = config.USE_COMPOSIO if use_composio is None else use_composio
    if use_composio:
        from scrapers.composio_fetcher import get_calendar_assignments as classroom_assignments
    else:
        from scrapers.google_scraper import get_calendar_assignments as classroom_assignments

    def _load_cached_canvas_extractions():
        cache_file = config.CACHE_DIR / "canvas_page_extractions.json"
        if not cache_file.exists():
            return []
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            res = []
            for page_key, tasks in data.items():
                if isinstance(tasks, list):
                    for item in tasks:
                        if isinstance(item, dict) and item.get("title") and item.get("due_date"):
                            t = dict(item)
                            t["official"] = True
                            if not t.get("id"):
                                key_str = f"{page_key}:{t.get(title)}:{t.get(due_date)}"
                                t["id"] = f"gen-{hashlib.md5(key_str.encode()).hexdigest()[:12]}"
                            if not t.get("course"):
                                t["course"] = "Canvas Coursework"
                            res.append(t)
            return res
        except Exception:
            return []

    candidates: list[Assignment] = []
    for source, loader in (
        ("canvas", canvas_assignments),
        ("canvas_pages", _load_cached_canvas_extractions),
        ("google_classroom", classroom_assignments),
        ("google_docs", lambda: google_docs_assignments(use_composio=use_composio)),
        ("notion", get_calendar_tasks),
    ):
        try:
            for payload in loader():
                assignment = _assignment_from_payload(payload, source)
                if assignment is not None:
                    candidates.append(assignment)
        except Exception as exc:
            logger.warning("Calendar source %s failed: %s", source, exc)

    import difflib

    import difflib

    merged_by_key: dict[str, Assignment] = {}

    for item in candidates:
        norm_title = _normalize_title_for_dedupe(item.title)
        norm_course = _normalize_course_for_dedupe(item.course)
        due_str = _assignment_date_str(item)
        if not norm_title or not due_str:
            continue

        matched_key = None
        for existing_key, existing_item in merged_by_key.items():
            ex_title = _normalize_title_for_dedupe(existing_item.title)
            ex_course = _normalize_course_for_dedupe(existing_item.course)
            ex_due = _assignment_date_str(existing_item)

            titles_match = (
                norm_title == ex_title
                or norm_title in ex_title
                or ex_title in norm_title
                or difflib.SequenceMatcher(None, norm_title, ex_title).ratio() >= 0.8
            )
            dates_match = _dates_are_close(due_str, ex_due, max_days=1)
            courses_match = (norm_course == ex_course or not norm_course or not ex_course or norm_course in ex_course or ex_course in norm_course)

            if titles_match and (dates_match or courses_match):
                matched_key = existing_key
                break

        if matched_key:
            existing = merged_by_key[matched_key]
            merged = _upsert_merge_assignments(existing, item)
            merged_by_key[matched_key] = merged
        else:
            merged_by_key[item.key] = item

    return list(merged_by_key.values())


class AssignmentCalendarService:
    """Plans and executes idempotent CalDAV updates with an optional Google mirror."""

    def __init__(self, store: SyncStore | None = None, caldav: CalDavCalendar | None = None, google: GoogleCalendarMirror | None = None) -> None:
        self.store = store or SyncStore()
        self.caldav = caldav or CalDavCalendar(
            config.ASSIGNMENT_CALDAV_COLLECTION_URL,
            config.ASSIGNMENT_CALDAV_USERNAME,
            config.ASSIGNMENT_CALDAV_PASSWORD,
        )
        persisted_calendar_id = self.store.setting("google_calendar_id")
        self.google = google or GoogleCalendarMirror.from_config(
            config.ASSIGNMENT_GOOGLE_CALENDAR_ID or persisted_calendar_id,
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.store.is_enabled(),
            "caldav_configured": self.caldav.configured(),
            "google_configured": self.google.configured(),
            "tracked_events": len(self.store.active_events()),
        }

    def enable(self) -> None:
        if not self.caldav.configured():
            raise CalendarSyncError("CalDAV configuration is incomplete. Add the URL, username, and password to .env.")
        self.caldav.ensure_collection()
        # CalDAV is the canonical calendar. The Google integration is only a
        # convenience mirror and must not prevent private-calendar syncing.
        if self.google.configured() or self.google.can_create_calendar():
            self.store.set_setting("google_calendar_id", self.google.ensure_calendar())
        self.store.set_enabled(True)

    def disable(self) -> None:
        self.store.set_enabled(False)

    def plan(self, assignments: Iterable[Assignment]) -> tuple[list[SyncAction], list[Assignment]]:
        current = {item.key: item for item in assignments}
        actions: list[SyncAction] = []
        proposals: list[Assignment] = []

        for item in current.values():
            if not item.official and self.store.is_suppressed(item):
                logger.debug("Suppressed proposed item by learned pattern: %s", item.title)
                continue
            existing = self.store.event(item.key)
            if not item.official:
                if existing and item.completed and not existing["completed"]:
                    actions.append(SyncAction("complete", item, existing["caldav_uid"], existing["google_event_id"]))
                elif not item.completed and (not existing or existing["fingerprint"] != item.fingerprint()):
                    proposals.append(item)
                continue
            if item.completed:
                if existing and not existing["completed"]:
                    actions.append(SyncAction("complete", item, existing["caldav_uid"], existing["google_event_id"]))
            elif not existing:
                actions.append(SyncAction("create", item))
            elif (
                existing["fingerprint"] != item.fingerprint()
                or existing["completed"]
                or (self.google.configured() and not existing["google_event_id"])
            ):
                actions.append(SyncAction("update", item, existing["caldav_uid"], existing["google_event_id"]))

        return actions, proposals

    def preview(self, assignments: Iterable[Assignment] | None = None) -> tuple[list[SyncAction], str | None, int]:
        actions, proposals = self.plan(assignments if assignments is not None else collect_assignments())
        batch_id = self.store.save_proposal(proposals) if proposals else None
        recommended = sum(1 for item in proposals if self.store.recommendation(item))
        return actions, batch_id, recommended

    def _apply(self, actions: Iterable[SyncAction]) -> int:
        if not self.store.is_enabled():
            raise CalendarSyncError("Assignment calendar sync is disabled. Preview changes, then enable it explicitly.")
        applied = 0
        for action in actions:
            if action.kind == "delete":
                if action.caldav_uid:
                    self.caldav.delete(action.caldav_uid)
                if action.google_event_id and self.google.configured():
                    self.google.delete(action.google_event_id)
                self.store.remove_event(action.assignment.key)
                applied += 1
                continue
            caldav_uid = self.caldav.upsert(action.assignment)
            google_event_id = action.google_event_id
            if self.google.configured():
                google_event_id = self.google.upsert(action.assignment, action.google_event_id)
            self.store.save_event(action.assignment, caldav_uid, google_event_id)
            applied += 1
        return applied

    def sync_official(self, assignments: Iterable[Assignment] | None = None) -> int:
        actions, _ = self.plan(assignments if assignments is not None else collect_assignments())
        return self._apply(actions)

    def sync_all(self, assignments: Iterable[Assignment] | None = None) -> int:
        """Automatically reconcile official assignments AND high-confidence deep-crawled deadlines."""
        all_items = list(assignments if assignments is not None else collect_assignments())
        actions, proposals = self.plan(all_items)
        
        # Merge high-confidence proposals into sync actions
        for item in proposals:
            existing = self.store.event(item.key)
            actions.append(
                SyncAction(
                    "update" if existing else "create",
                    item,
                    existing["caldav_uid"] if existing else None,
                    existing["google_event_id"] if existing else None,
                )
            )
        return self._apply(actions)

    def approve_proposal(self, batch_id: str) -> int:
        manual = self.store.proposal(batch_id)
        self.store.resolve_proposal(batch_id, approved=True)
        # Manual items only exist in this approval batch, so construct creates
        # explicitly rather than deleting unrelated official source events.
        manual_actions: list[SyncAction] = []
        for item in manual:
            existing = self.store.event(item.key)
            manual_actions.append(SyncAction("update" if existing else "create", item, existing["caldav_uid"] if existing else None, existing["google_event_id"] if existing else None))
        return self._apply(manual_actions)

    def reject_proposal(self, batch_id: str) -> None:
        self.store.resolve_proposal(batch_id, approved=False)

    def prune_stale_and_duplicate_events(self, valid_assignments: Iterable[Assignment] | None = None) -> int:
        """Purge stale, deleted, or unverified events from CalDAV and SQLite store."""
        if valid_assignments is None:
            valid_assignments = collect_assignments()
        valid_keys = {item.key for item in valid_assignments}
        existing_events = self.store.active_events()
        pruned = 0
        for row in existing_events:
            key = row["source_key"]
            if key not in valid_keys:
                caldav_uid = row["caldav_uid"]
                google_event_id = row["google_event_id"]
                if caldav_uid and self.caldav.configured():
                    try:
                        self.caldav.delete(caldav_uid)
                    except Exception as exc:
                        logger.debug("Could not delete CalDAV event %s: %s", caldav_uid, exc)
                if google_event_id and self.google.configured():
                    try:
                        self.google.delete(google_event_id)
                    except Exception as exc:
                        logger.debug("Could not delete Google event %s: %s", google_event_id, exc)
                self.store.remove_event(key)
                pruned += 1
        return pruned

    def get_all_events(self) -> list[dict]:
        """Return all active calendar events tracked in the store."""
        return self.store.active_events()


def format_preview(actions: list[SyncAction], batch_id: str | None, recommended: int) -> str:
    counts: dict[str, int] = {}
    for action in actions:
        counts[action.kind] = counts.get(action.kind, 0) + 1
    official = ", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items())) or "No official assignment changes"
    message = f"Assignment calendar preview\nOfficial school work: {official}."
    if batch_id:
        rec_str = f" ({recommended} recommended from your prior approvals)" if recommended > 0 else " (unsure / new patterns awaiting review)"
        message += f"\nDeep crawl & document proposals: batch {batch_id}{rec_str}."
    return message





# Alias for backward compatibility / direct verification:
AssignmentCalendar = AssignmentCalendarService
