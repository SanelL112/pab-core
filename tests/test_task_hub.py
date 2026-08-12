from datetime import date, datetime, timedelta, timezone

from ai_processor import _deterministic_digest, _is_unusable_model_output
from scrapers.canvas_scraper import (
    _assignment_is_actionable,
    _get_upcoming_assignments,
    _is_recent_canvas_update,
    _submission_state,
    _supported_canvas_study_file,
)
from scrapers.composio_fetcher import _classroom_work_is_actionable as composio_classroom_work_is_actionable
from scrapers.google_scraper import _classroom_work_is_actionable as google_classroom_work_is_actionable
from scrapers.notion_client import determine_task_priority


def test_canvas_assignment_filter_rejects_old_due_work(monkeypatch):
    monkeypatch.setenv("CANVAS_ASSIGNMENT_OVERDUE_GRACE_DAYS", "7")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert not _assignment_is_actionable({"due_at": "2026-05-01T23:59:00Z"}, now)
    assert _assignment_is_actionable({"due_at": "2026-07-26T23:59:00Z"}, now)
    assert _assignment_is_actionable({"due_at": "2026-07-19T23:59:00Z"}, now)


def test_canvas_assignment_filter_keeps_only_recent_undated_items(monkeypatch):
    monkeypatch.setenv("CANVAS_NO_DUE_UPDATE_DAYS", "21")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert _assignment_is_actionable({"updated_at": "2026-07-10T00:00:00Z"}, now)
    assert not _assignment_is_actionable({"updated_at": "2026-02-13T00:00:00Z"}, now)


def test_canvas_content_filter_hides_old_pages_and_announcements(monkeypatch):
    monkeypatch.setenv("CANVAS_CONTENT_UPDATE_DAYS", "21")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)

    assert _is_recent_canvas_update({"updated_at": "2026-07-20T00:00:00Z"}, "updated_at", now=now)
    assert not _is_recent_canvas_update({"updated_at": "2026-02-13T00:00:00Z"}, "updated_at", now=now)


def test_canvas_action_plan_uses_completion_not_scores():
    now = datetime.now(timezone.utc)

    class FakeCanvas:
        def get_favorite_courses(self):
            return [{"id": "1", "name": "Physics"}]

        def get_paginated(self, _path, max_pages=1):
            return [
                {
                    "name": "Lab report", "due_at": (now - timedelta(days=1)).isoformat(),
                    "submission": {"missing": True},
                },
                {
                    "name": "Chapter quiz", "due_at": (now + timedelta(days=2)).isoformat(),
                    "submission": {},
                },
                {
                    "name": "Practice problems", "due_at": (now - timedelta(days=1)).isoformat(),
                    "submission": {"submitted_at": now.isoformat(), "score": 0},
                },
            ]

    result = _get_upcoming_assignments(FakeCanvas())

    assert "Lab report" in result and "**Missing**" in result
    assert "Chapter quiz" in result and "Due soon" in result
    assert "Practice problems" in result and "Recently completed" in result
    assert "score" not in result.lower()
    assert _submission_state({"submission": {"submitted_at": now.isoformat()}}, now) == "Completed"
    assert _supported_canvas_study_file({"display_name": "review.docx"})
    assert not _supported_canvas_study_file({"display_name": "lecture.mp4"})


def test_google_classroom_filters_expired_work_across_both_fetchers(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLASSROOM_ASSIGNMENT_OVERDUE_GRACE_DAYS", "7")
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    old_work = {"dueDate": {"year": 2026, "month": 6, "day": 1}}
    upcoming_work = {"dueDate": {"year": 2026, "month": 7, "day": 29}}

    for predicate in (google_classroom_work_is_actionable, composio_classroom_work_is_actionable):
        assert not predicate(old_work, now)
        assert predicate(upcoming_work, now)


def test_deadline_priority_is_meaningful(monkeypatch):
    monkeypatch.setenv("TASK_HIGH_PRIORITY_DAYS", "3")
    monkeypatch.setenv("TASK_MEDIUM_PRIORITY_DAYS", "7")
    today = date(2026, 7, 25)

    assert determine_task_priority("2026-07-24", "low", today=today) == "high"
    assert determine_task_priority("2026-07-29", "low", today=today) == "medium"
    assert determine_task_priority("2026-08-20", "medium", today=today) == "medium"
    assert determine_task_priority("2026-08-20", "high", today=today) == "high"
    assert determine_task_priority(None, "unknown", today=today) == "medium"


def test_digest_fallback_is_actionable_when_the_model_is_unavailable():
    summaries = {
        "canvas": "[Calculus] Problem Set 4 - Due: 2099-07-26 (Updated: 2099-07-20)",
        "classroom": "No recent published coursework found.",
        "classroom_announcements": "[SAT Prep]: The practice-test room changed to 204.",
        "gmail": "⚠️ Local inference unavailable and cloud fallback disabled.",
        "groupme": "No urgent groupme updates.",
        "gdocs": "No recent Google Docs found.",
    }

    digest, tasks = _deterministic_digest(summaries)

    assert "⚡ **Needs attention**" in digest
    assert "Problem Set 4 — due 2099-07-26" in digest
    assert "practice-test room changed" in digest
    assert "Local inference unavailable" not in digest
    assert tasks[0]["title"] == "Problem Set 4"
    assert tasks[0]["course"] == "Calculus"
    assert _is_unusable_model_output("⚠️ Local inference unavailable and cloud fallback disabled.")
    assert _is_unusable_model_output("I'm sorry, but I can't assemble that.")
    assert not _is_unusable_model_output("📚 **Canvas**\n• Assignment due tomorrow")
