"""Turn explicit Google Docs deadlines into approval-gated calendar proposals."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import config


_MONTH = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
)
_WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_DATE_TOKEN = (
    rf"(?:\d{{4}}-\d{{1,2}}-\d{{1,2}}|\d{{1,2}}/\d{{1,2}}(?:/\d{{2,4}})?|"
    rf"(?:{_MONTH})\s+\d{{1,2}}(?:,?\s+\d{{2,4}})?|"
    rf"(?:{'|'.join(_WEEKDAY_NAMES)}))"
)
_DEADLINE_LABEL = re.compile(
    r"\b(?:due(?:\s+date)?|deadline|turn\s+in|submit(?:\s+by)?)\b",
    re.IGNORECASE,
)
_DATE_PATTERN = re.compile(rf"\b{_DATE_TOKEN}\b", re.IGNORECASE)


def _parse_deadline(value: str, today: date) -> date | None:
    """Parse only an explicit, unambiguous calendar date or weekday."""
    token = " ".join(value.strip().replace(",", " ").split())
    lowered = token.lower()
    if lowered in _WEEKDAY_NAMES:
        return today + timedelta(days=(_WEEKDAY_NAMES[lowered] - today.weekday()) % 7)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return date.fromisoformat(token) if fmt == "%Y-%m-%d" else datetime.strptime(token, fmt).date()
        except ValueError:
            pass

    if "/" in token:
        try:
            month, day = (int(piece) for piece in token.split("/"))
            parsed = date(today.year, month, day)
            return parsed if parsed >= today - timedelta(days=31) else date(today.year + 1, month, day)
        except ValueError:
            return None

    for fmt in ("%B %d %Y", "%b %d %Y", "%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(token, fmt).date()
            if "%Y" not in fmt:
                parsed = parsed.replace(year=today.year)
                if parsed < today - timedelta(days=31):
                    parsed = parsed.replace(year=today.year + 1)
            return parsed
        except ValueError:
            pass
    return None


def _title_for_deadline(
    lines: list[str], index: int, match: re.Match[str], fallback: str, *, prefer_previous: bool = False
) -> str:
    prefix = "" if prefer_previous else lines[index][:match.start()].strip(" \t:-—")
    prefix = re.sub(r"^(?:assignment|homework|task)\s*[:\-]?\s*", "", prefix, flags=re.IGNORECASE)
    if len(prefix) >= 3:
        return prefix[:140]
    for previous in reversed(lines[:index]):
        candidate = previous.strip(" \t:-—")
        if candidate and not _DEADLINE_LABEL.search(candidate):
            return candidate[:140]
    return fallback[:140] or "Google Docs assignment"


def _task_type(title: str) -> str:
    lowered = title.lower()
    if any(word in lowered for word in ("test", "quiz", "exam")):
        return "Test"
    if "project" in lowered:
        return "Project"
    if "read" in lowered:
        return "Reading"
    return "Assignment"


def extract_google_doc_assignments(documents: Iterable[dict[str, Any]], today: date | None = None) -> list[dict[str, Any]]:
    """Extract explicit due-date lines from document records.

    This deliberately avoids broad AI inference: only text that labels a date
    as due/a deadline/turn-in/submission becomes a proposal, and never an
    automatic calendar write.
    """
    today = today or date.today()
    try:
        overdue_grace = max(0, int(config.get_setting("GOOGLE_DOC_CALENDAR_OVERDUE_GRACE_DAYS", "7")))
    except ValueError:
        overdue_grace = 7
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for document in documents:
        doc_id = str(document.get("id") or document.get("document_id") or "").strip()
        doc_title = str(document.get("title") or document.get("name") or "Google Docs assignment").strip()
        content = document.get("content") or document.get("text") or ""
        if not doc_id or not isinstance(content, str) or not content.strip():
            continue
        url = str(document.get("url") or document.get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit")
        course = str(document.get("course") or "Google Docs").strip() or "Google Docs"
        lines = [" ".join(line.split()) for line in content.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            for match in _DEADLINE_LABEL.finditer(line):
                nearby_dates = list(_DATE_PATTERN.finditer(line))
                if not nearby_dates:
                    continue
                date_match = min(
                    nearby_dates,
                    key=lambda candidate: min(
                        abs(candidate.start() - match.end()), abs(match.start() - candidate.end())
                    ),
                )
                gap = min(abs(date_match.start() - match.end()), abs(match.start() - date_match.end()))
                # Permit prose such as "deadline to submit by January 15" and
                # date-first headings such as "January 15 — deadline".
                if gap > 120:
                    continue
                due = _parse_deadline(date_match.group(0), today)
                if due is None or due < today - timedelta(days=overdue_grace):
                    continue
                title = _title_for_deadline(
                    lines,
                    index,
                    match,
                    doc_title,
                    prefer_previous=date_match.end() <= match.start(),
                )
                dedupe_key = (doc_id, title.lower(), due.isoformat())
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                digest = hashlib.sha256("|".join(dedupe_key).encode()).hexdigest()[:16]
                results.append({
                    "id": f"{doc_id}:{digest}",
                    "title": title,
                    "course": course,
                    "due_date": due.isoformat(),
                    "url": url,
                    "task_type": _task_type(title),
                    "status": "Not started",
                    "official": True,
                })
                # A sentence may contain both "deadline" and "submit";
                # they describe the same date, not two assignments.
                break
    return results


def get_calendar_assignments(use_composio: bool | None = None) -> list[dict[str, Any]]:
    """Fetch document records through the configured Google connection."""
    use_composio = config.USE_COMPOSIO if use_composio is None else use_composio
    if use_composio:
        from scrapers.composio_fetcher import get_recent_google_doc_records
    else:
        from scrapers.google_scraper import get_recent_google_doc_records
    return extract_google_doc_assignments(get_recent_google_doc_records())
