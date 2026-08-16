from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from scrapers.assignment_calendar import (
    Assignment,
    AssignmentCalendarService,
    CalDavCalendar,
    CalendarSyncError,
    GoogleCalendarMirror,
    SyncStore,
    _assignment_from_payload,
    collect_assignments,
    render_ical_event,
)
from scrapers.google_docs_calendar import extract_google_doc_assignments


class FakeCalDav:
    def __init__(self) -> None:
        self.ensured = 0
        self.events: dict[str, Assignment] = {}
        self.deleted: list[str] = []

    def configured(self) -> bool:
        return True

    def ensure_collection(self) -> None:
        self.ensured += 1

    def upsert(self, assignment: Assignment) -> str:
        uid = assignment.event_uid()
        self.events[uid] = assignment
        return uid

    def delete(self, uid: str) -> None:
        self.deleted.append(uid)
        self.events.pop(uid, None)


class FakeGoogle:
    def __init__(self) -> None:
        self.calendar_id = "assignment-calendar"
        self.events: dict[str, Assignment] = {}
        self.deleted: list[str] = []

    def configured(self) -> bool:
        return True

    def ensure_calendar(self) -> str:
        return self.calendar_id

    def upsert(self, assignment: Assignment, event_id: str | None = None) -> str:
        event_id = event_id or f"google-{assignment.event_uid()}"
        self.events[event_id] = assignment
        return event_id

    def delete(self, event_id: str) -> None:
        self.deleted.append(event_id)
        self.events.pop(event_id, None)


class FakeGoogleUnavailable:
    def configured(self) -> bool:
        return False

    def can_create_calendar(self) -> bool:
        return False


def _service(tmp_path: Path) -> tuple[AssignmentCalendarService, FakeCalDav, FakeGoogle]:
    caldav = FakeCalDav()
    google = FakeGoogle()
    service = AssignmentCalendarService(
        store=SyncStore(tmp_path / "assignment-calendar.sqlite3"),
        caldav=caldav,
        google=google,
    )
    return service, caldav, google


def _official(**changes: object) -> Assignment:
    payload = {
        "source": "canvas",
        "external_id": "biology:42",
        "title": "Cell cycle quiz",
        "course": "Biology",
        "due_at": "2026-07-24T15:00:00Z",
        "official": True,
    }
    payload.update(changes)
    return Assignment(**payload)


def test_ical_rendering_has_expected_due_dates_and_reminders():
    timed = render_ical_event(_official())
    assert "DTSTART:20260724T150000Z" in timed
    assert "TRIGGER:-P1D" in timed
    assert "TRIGGER:-PT1H" in timed
    assert "X-PAB-OWNER:assignment-calendar" in timed

    date_only = render_ical_event(
        Assignment(
            source="google_classroom",
            external_id="chemistry:7",
            title="Lab report",
            course="Chemistry",
            due_date="2026-07-24",
            official=True,
        )
    )
    assert "DTSTART;VALUE=DATE:20260724" in date_only
    assert "TRIGGER;VALUE=DATE-TIME:20260724T110000Z" in date_only

    completed = render_ical_event(_official(status="Completed"))
    assert "SUMMARY:Completed: Cell cycle quiz" in completed
    assert "BEGIN:VALARM" not in completed
    assert GoogleCalendarMirror._payload(_official(status="Completed"))["reminders"] == {
        "useDefault": False,
        "overrides": [],
    }


def test_caldav_requires_an_actual_collection_url():
    assert not CalDavCalendar("", "owner", "secret").configured()


def test_caldav_existing_collection_conflict_is_idempotent():
    class Response:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    class Session:
        def __init__(self) -> None:
            self.methods: list[str] = []

        def request(self, method: str, *_args: object, **_kwargs: object) -> Response:
            self.methods.append(method)
            return Response(409 if method == "MKCALENDAR" else 207)

    calendar = CalDavCalendar("http://calendar.local/assignments/", "owner", "secret")
    session = Session()
    calendar.session = session  # type: ignore[assignment]

    calendar.ensure_collection()
    assert session.methods == ["MKCALENDAR", "PROPFIND"]


def test_google_calendar_can_be_created_from_a_verified_action():
    mirror = GoogleCalendarMirror(
        calendar_create_tool="calendar-create",
        create_tool="event-create",
        update_tool="event-update",
        delete_tool="event-delete",
    )
    calls: list[tuple[str, dict]] = []

    def fake_call(tool: str, arguments: dict) -> dict:
        calls.append((tool, arguments))
        return {"calendar": {"id": "assignments-id"}}

    mirror._call = fake_call  # type: ignore[method-assign]
    assert mirror.ensure_calendar() == "assignments-id"
    assert mirror.configured()
    assert calls == [("calendar-create", {"summary": "Assignments"})]


def test_official_sync_is_disabled_until_enabled_and_idempotent(tmp_path: Path):
    service, caldav, google = _service(tmp_path)
    assignment = _official()

    assert not service.status()["enabled"]
    with pytest.raises(CalendarSyncError, match="disabled"):
        service.sync_official([assignment])

    service.enable()
    assert caldav.ensured == 1
    assert service.sync_official([assignment]) == 1
    assert service.sync_official([assignment]) == 0
    assert len(caldav.events) == 1
    assert len(google.events) == 1

    completed = _official(status="Completed")
    assert service.sync_official([completed]) == 1
    assert next(iter(caldav.events.values())).completed
    assert "BEGIN:VALARM" not in render_ical_event(next(iter(caldav.events.values())))
    assert service.plan([])[0] == []


def test_caldav_sync_does_not_require_a_google_mirror(tmp_path: Path):
    caldav = FakeCalDav()
    service = AssignmentCalendarService(
        store=SyncStore(tmp_path / "assignment-calendar.sqlite3"),
        caldav=caldav,
        google=FakeGoogleUnavailable(),
    )

    service.enable()
    assert service.status()["enabled"]
    assert service.sync_official([_official()]) == 1
    assert len(caldav.events) == 1


def test_manual_tasks_remain_approval_gated_after_approval(tmp_path: Path):
    service, caldav, google = _service(tmp_path)
    manual = Assignment(
        source="notion",
        external_id="task-1",
        title="Review notes",
        course="Biology",
        due_date="2026-07-25",
    )

    actions, batch_id, recommended = service.preview([manual])
    assert actions == []
    assert batch_id
    assert recommended == 0

    service.enable()
    assert service.approve_proposal(batch_id) == 1
    stored = service.store.event(manual.key)
    assert stored is not None
    assert stored["official"] == 0
    assert len(caldav.events) == 1
    assert len(google.events) == 1

    changed = Assignment(**{**manual.__dict__, "due_date": "2026-07-26"})
    actions, next_batch_id, _ = service.preview([changed])
    assert actions == []
    assert next_batch_id


def test_payload_parsing_preserves_source_and_date_only_deadline():
    assignment = _assignment_from_payload(
        {
            "id": "course:work",
            "title": "Unit test",
            "course": "Testing",
            "dueDate": {"year": 2026, "month": 7, "day": 24},
            "official": True,
        },
        "google_classroom",
    )
    assert assignment is not None
    assert assignment.source == "google_classroom"
    assert assignment.due_date == "2026-07-24"
    assert assignment.is_date_only


def test_google_docs_deadlines_become_official_calendar_items():
    proposals = extract_google_doc_assignments(
        [{
            "id": "doc-123",
            "title": "English assignments",
            "url": "https://docs.google.com/document/d/doc-123/edit",
            "content": "\n".join(["Literary analysis outline", "Deadline to submit it by August 5, 2026"]),
        }],
        today=date(2026, 7, 29),
    )

    assert len(proposals) == 1
    assert proposals[0]["title"] == "Literary analysis outline"
    assert proposals[0]["course"] == "Google Docs"
    assert proposals[0]["due_date"] == "2026-08-05"
    assert proposals[0]["url"] == "https://docs.google.com/document/d/doc-123/edit"
    # Google Docs extractions are treated as official (Sanel's decision in 7e0a23c).
    assert proposals[0]["official"] is True


def test_google_docs_dates_without_a_due_label_are_ignored():
    assert extract_google_doc_assignments(
        [{"id": "doc-123", "title": "Notes", "content": "We met on August 5, 2026."}],
        today=date(2026, 7, 29),
    ) == []


def test_collection_honors_the_native_classroom_selector():
    classroom = {
        "id": "biology:work",
        "title": "Native Classroom work",
        "course": "Biology",
        "due_date": "2026-07-24",
        "official": True,
    }
    with patch("scrapers.canvas_scraper.get_calendar_assignments", return_value=[]), patch(
        "scrapers.google_scraper.get_calendar_assignments", return_value=[classroom]
    ) as native_classroom, patch(
        "scrapers.google_docs_calendar.get_calendar_assignments", return_value=[]
    ) as google_docs, patch("scrapers.notion_client.get_calendar_tasks", return_value=[]):
        assignments = collect_assignments(use_composio=False)

    native_classroom.assert_called_once_with()
    google_docs.assert_called_once_with(use_composio=False)
    assert [item.title for item in assignments] == ["Native Classroom work"]


def test_collection_includes_google_doc_deadlines_as_non_official_tasks():
    google_doc = {
        "id": "doc-123:deadline",
        "title": "Lab reflection",
        "course": "Google Docs",
        "due_date": "2026-08-05",
        "official": False,
    }
    with patch("scrapers.canvas_scraper.get_calendar_assignments", return_value=[]), patch(
        "scrapers.google_scraper.get_calendar_assignments", return_value=[]
    ), patch(
        "scrapers.google_docs_calendar.get_calendar_assignments", return_value=[google_doc]
    ) as google_docs, patch("scrapers.notion_client.get_calendar_tasks", return_value=[]):
        assignments = collect_assignments(use_composio=False)

    google_docs.assert_called_once_with(use_composio=False)
    assert len(assignments) == 1
    assert assignments[0].source == "google_docs"
    assert not assignments[0].official
