#!/usr/bin/env python3
"""Notify once when new Canvas courses appear (fall roster landing).

Between terms the only active Canvas courses are prior-year clubs sitting in
"Default Term", so the assignment calendar has nothing to sync.  Rather than
checking by hand, this poller compares the current course set against a stored
snapshot and sends a single Telegram message the first time something new shows
up — a published enrollment, a pending invitation, or a newly visible course.

Design notes:
* Reads through the existing canvas-browser daemon, so it reuses the warm
  ClassLink session and never handles credentials itself.
* Silent by default.  No message when nothing changed, so it is safe to run
  often and will not become background noise.
* State is a snapshot of known course IDs, so a course is announced exactly
  once even if it stays in the list for months.
* A course that disappears is recorded silently: end-of-term removal is not
  something worth a notification.

Exit codes: 0 = ran (with or without a change), 1 = could not check.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from scrapers.canvas_scraper import (  # noqa: E402
    CanvasBrowserClient,
    CanvasSessionError,
    CanvasSignInRequired,
)

logger = logging.getLogger("canvas_new_course_watch")

SNAPSHOT_PATH = config.STATE_DIR / "canvas_known_courses.json"

# Clubs live in "Default Term" with no start date.  A real scheduled class comes
# with a named term, so it is worth calling out separately in the message.
CLUB_TERM_NAMES = {"default term", ""}

QUERIES = (
    ("active", "/api/v1/courses?enrollment_state=active&include[]=term&per_page=100"),
    ("pending", "/api/v1/courses?enrollment_state=invited_or_pending&include[]=term&per_page=100"),
)


def _load_snapshot() -> dict[str, Any]:
    try:
        with SNAPSHOT_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"known_ids": [], "initialized": False}
    if not isinstance(data, dict):
        return {"known_ids": [], "initialized": False}
    ids = data.get("known_ids")
    return {
        "known_ids": [str(x) for x in ids] if isinstance(ids, list) else [],
        "initialized": bool(data.get("initialized")),
    }


def _save_snapshot(course_ids: list[str]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"known_ids": sorted(set(course_ids)), "initialized": True}
    tmp = SNAPSHOT_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    tmp.replace(SNAPSHOT_PATH)
    SNAPSHOT_PATH.chmod(0o600)


def _term_name(course: dict[str, Any]) -> str:
    term = course.get("term")
    if isinstance(term, dict):
        return str(term.get("name") or "")
    return ""


def _collect_courses() -> dict[str, dict[str, Any]]:
    """Return {course_id: course} across active and pending enrollments."""
    found: dict[str, dict[str, Any]] = {}
    with CanvasBrowserClient() as canvas:
        for state, query in QUERIES:
            try:
                courses = canvas.get_paginated(query, max_pages=3)
            except CanvasSessionError as exc:
                # A partial read must not be mistaken for "courses disappeared",
                # so surface it rather than silently snapshotting a short list.
                raise CanvasSessionError(f"{state} course query failed: {exc}") from exc
            for course in courses:
                course_id = course.get("id")
                if course_id is None:
                    continue
                # A course with no readable name is one we lack access to.
                if not course.get("name"):
                    continue
                record = dict(course)
                record["_enrollment_state"] = state
                found[str(course_id)] = record
    return found


def _format_message(new_courses: list[dict[str, Any]]) -> str:
    scheduled = [c for c in new_courses if _term_name(c).strip().lower() not in CLUB_TERM_NAMES]
    clubs = [c for c in new_courses if c not in scheduled]

    if scheduled:
        headline = f"New Canvas classes detected ({len(scheduled)})"
    else:
        headline = f"New Canvas courses detected ({len(new_courses)})"

    lines = [headline, ""]
    for course in scheduled + clubs:
        name = str(course.get("name") or "Unnamed course")
        term = _term_name(course) or "no term"
        state = "pending invite" if course["_enrollment_state"] == "pending" else "active"
        lines.append(f"- {name} ({term}, {state})")

    if scheduled:
        lines.append("")
        lines.append("The assignment calendar will begin syncing due dates automatically.")
    return "\n".join(lines)


def _send_telegram(text: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("Telegram is not configured; cannot deliver the notification.")
        return False
    import httpx

    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:3500]},
            timeout=20.0,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        # Never log the token-bearing URL or the response body.
        logger.error("Telegram delivery failed (%s).", type(exc).__name__)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report findings without sending or saving")
    parser.add_argument("--reset", action="store_true", help="re-snapshot current courses as the baseline")
    parser.add_argument("--verbose", action="store_true", help="log the full current course list")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        courses = _collect_courses()
    except CanvasSignInRequired as exc:
        logger.error("Canvas sign-in required: %s", exc)
        return 1
    except CanvasSessionError as exc:
        logger.error("Canvas unavailable: %s", exc)
        return 1

    if not courses:
        # Zero readable courses means the session or API failed in a way that did
        # not raise.  Treating it as "everything vanished" would wipe the
        # snapshot and cause a false alert storm on the next run.
        logger.error("No readable courses returned; refusing to update the snapshot.")
        return 1

    current_ids = sorted(courses)
    if args.verbose:
        for course_id in current_ids:
            course = courses[course_id]
            logger.info("  %s | %s | term=%s | %s", course_id, course.get("name"),
                        _term_name(course) or "-", course["_enrollment_state"])

    if args.reset:
        if not args.dry_run:
            _save_snapshot(current_ids)
        logger.info("Baseline reset to %d known course(s).", len(current_ids))
        return 0

    snapshot = _load_snapshot()
    known = set(snapshot["known_ids"])

    if not snapshot["initialized"]:
        # First run establishes the baseline.  Announcing all 7 existing club
        # courses as "new" would be noise, not signal.
        if not args.dry_run:
            _save_snapshot(current_ids)
        logger.info("Baseline established with %d existing course(s). Future additions will notify.", len(current_ids))
        return 0

    new_ids = [cid for cid in current_ids if cid not in known]
    if not new_ids:
        logger.info("No new courses (%d known).", len(known))
        return 0

    new_courses = [courses[cid] for cid in new_ids]
    message = _format_message(new_courses)

    if args.dry_run:
        logger.info("[DRY RUN] would send:\n%s", message)
        return 0

    delivered = _send_telegram(message)
    if delivered:
        # Only record the course as announced once the message actually left,
        # so a delivery failure retries on the next run instead of going quiet.
        _save_snapshot(current_ids)
        logger.info("Notified about %d new course(s).", len(new_ids))
        return 0

    logger.error("Notification not delivered; snapshot left unchanged for retry.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
