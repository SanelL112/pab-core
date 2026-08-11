"""Synchronize school assignments to private CalDAV and Google calendars.

The local CalDAV calendar is canonical. Google is a one-way mirror accessed
through the existing Composio connection. This module never writes externally
until its local enabled flag is set by an authenticated Telegram action.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
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
            for item in assignments:
                db.execute(
                    """INSERT INTO approval_preferences(source, course, task_type, approved_count, rejected_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(source, course, task_type) DO UPDATE SET
                      approved_count = approved_count + excluded.approved_count,
                      rejected_count = rejected_count + excluded.rejected_count""",
                    (item.source, item.course, item.task_type, 1 if approved else 0, 0 if approved else 1),
                )
            db.execute("UPDATE proposals SET resolved_at = ? WHERE batch_id = ?", (datetime.now(timezone.utc).isoformat(), batch_id))

    def recommendation(self, item: Assignment) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT approved_count, rejected_count FROM approval_preferences WHERE source = ? AND course = ? AND task_type = ?",
                (item.source, item.course, item.task_type),
            ).fetchone()
        return bool(row and row["approved_count"] > row["rejected_count"])


def _assignment_from_payload(payload: dict[str, Any], source: str) -> Assignment | None:
    external_id = str(payload.get("id") or payload.get("external_id") or "").strip()
    title = str(payload.get("title") or payload.get("name") or "").strip()
    due_at = payload.get("due_at") or payload.get("dueAt")
    due_date = payload.get("due_date") or payload.get("dueDate")
    if isinstance(due_date, dict):
        try:
            due_date = date(int(due_date["year"]), int(due_date["month"]), int(due_date["day"])).isoformat()
        except (KeyError, TypeError, ValueError):
            due_date = None
    if not external_id or not title or not (due_at or due_date):
        return None
    task_type = str(payload.get("task_type") or "Assignment")
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
        official=bool(payload.get("official", source in {"canvas", "google_classroom"})),
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

    candidates: list[Assignment] = []
    for source, loader in (
        ("canvas", canvas_assignments),
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

    # A Notion row copied from Canvas/Classroom must not get a second event.
    official_keys = {(item.title.lower(), item.course.lower(), str(item.local_due())) for item in candidates if item.official}
    unique: dict[str, Assignment] = {}
    for item in candidates:
        duplicate = item.source == "notion" and (item.title.lower(), item.course.lower(), str(item.local_due())) in official_keys
        if not duplicate:
            unique[item.key] = item
    return list(unique.values())


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

        # A missing record is ambiguous: the source may have been unreachable
        # or only partially returned. Never turn a failed read into mass event
        # deletion. Explicit cleanup may use the owned-event delete path later.

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


def format_preview(actions: list[SyncAction], batch_id: str | None, recommended: int) -> str:
    counts: dict[str, int] = {}
    for action in actions:
        counts[action.kind] = counts.get(action.kind, 0) + 1
    official = ", ".join(f"{kind}: {count}" for kind, count in sorted(counts.items())) or "No official assignment changes"
    message = f"Assignment calendar preview\nOfficial school work: {official}."
    if batch_id:
        message += f"\nNotion, Google Docs, and manual tasks awaiting review: batch {batch_id} ({recommended} recommended from prior approvals)."
    return message
