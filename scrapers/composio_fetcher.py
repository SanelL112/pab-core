"""
composio_fetcher.py - Drop-in replacement for Google scraper functions using Composio MCP.

Provides the same function signatures as scrapers/google_scraper.py and
scrapers/canvas_scraper.py but routes through Composio's OAuth-managed connections
(6-month refresh instead of 7-day tokens).

Usage:
    from scrapers.composio_fetcher import get_unread_emails, get_classroom_assignments, ...

The existing scrapers remain in the codebase — swap the import in main.py to use this.
"""

import json
import logging
import http.client
import os
import re
import time
import datetime
from typing import Optional

import config
from bot.storage import AtomicJSONStore, StorageError

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
COMPOSIO_HOST = "connect.composio.dev"
COMPOSIO_PATH = "/mcp"
CANVAS_CACHE_TTL = 3600 * 24 * 7  # 1 week — course list rarely changes

# Entity IDs used per app
ENTITY_GOOGLE = "default"          # Google apps use "default" entity
ENTITY_CANVAS = "canvas_ionone-arided"  # Canvas entity ID


# ── Internal helpers ────────────────────────────────────────────────────────

def _load_token() -> Optional[str]:
    """Load a private Composio token without logging its path or contents."""
    try:
        token_path = config.COMPOSIO_TOKEN_PATH
        if token_path.is_symlink():
            raise OSError("token path is a symlink")
        with token_path.open(encoding="utf-8") as f:
            tokens = json.load(f)
        token = tokens.get("access_token") if isinstance(tokens, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token missing")
        return token.strip()
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
        logger.info("Composio token unavailable (%s).", type(exc).__name__)
        return None


def _failure() -> dict:
    """Public-safe integration result; provider errors are logged separately."""
    return {"successful": False, "data": {"message": "Service temporarily unavailable"}}


def _response_from_rpc(payload: object) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        try:
            inner = json.loads(item.get("text", ""))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(inner, dict):
            continue
        results = inner.get("data", {}).get("results", [])
        if not isinstance(results, list) or not results:
            continue
        response = results[0].get("response") if isinstance(results[0], dict) else None
        if isinstance(response, dict) and response.get("successful"):
            return response
    return None


def _call_mcp(tool_slug: str, arguments: dict, entity_id: str = ENTITY_GOOGLE) -> Optional[dict]:
    """
    Call a Composio MCP tool via the JSON-RPC endpoint.
    Returns the response data dict (successful: bool, data: {...}).
    """
    token = _load_token()
    if not token:
        return _failure()

    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "COMPOSIO_MULTI_EXECUTE_TOOL",
            "arguments": {
                "tools": [{
                    "tool_slug": tool_slug,
                    "arguments": arguments,
                    "entityId": entity_id
                }],
                "thought": f"Fetch via {tool_slug}",
                "sync_response_to_workbench": False
            }
        }
    })
    if len(payload.encode("utf-8")) > 128_000:
        logger.warning("Refused oversized Composio request for %s.", tool_slug)
        return _failure()

    conn: http.client.HTTPSConnection | None = None
    try:
        conn = http.client.HTTPSConnection(COMPOSIO_HOST, timeout=15)
        conn.request("POST", COMPOSIO_PATH, body=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        })
        resp = conn.getresponse()
        if not 200 <= resp.status < 300:
            logger.warning("Composio MCP returned HTTP %s for %s.", resp.status, tool_slug)
            return _failure()
        raw = resp.read(config.COMPOSIO_MAX_RESPONSE_BYTES + 1)
        if len(raw) > config.COMPOSIO_MAX_RESPONSE_BYTES:
            logger.warning("Composio MCP response exceeded size limit for %s.", tool_slug)
            return _failure()
        text = raw.decode("utf-8", "replace")

        # The MCP gateway may return JSON or SSE.  A malformed event must not
        # break a Telegram command or expose the provider's raw error body.
        candidates: list[str] = [text] if text.lstrip().startswith("{") else []
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                candidates.append(line[6:].strip())
        for candidate in candidates:
            try:
                response = _response_from_rpc(json.loads(candidate))
            except json.JSONDecodeError:
                continue
            if response is not None:
                return response
        logger.warning("Composio MCP returned no usable result for %s.", tool_slug)
        return _failure()
    except (OSError, http.client.HTTPException, ValueError) as exc:
        logger.warning("Composio MCP call failed for %s (%s).", tool_slug, type(exc).__name__)
        return _failure()
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass


def _strip_html(text: str) -> str:
    '''Strip HTML tags and decode common entities for clean Telegram output.'''
    text = re.sub(r'<[^>]+>', ' ', text)       # Strip tags
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)    # Strip &amp; &nbsp; etc.
    text = re.sub(r'\s+', ' ', text)             # Collapse whitespace
    return text.strip()


def _classroom_work_is_actionable(work: dict, now: datetime.datetime | None = None) -> bool:
    """Keep overdue coursework from endlessly re-entering the task pipeline."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    overdue_grace = max(0, int(os.getenv("GOOGLE_CLASSROOM_ASSIGNMENT_OVERDUE_GRACE_DAYS", "7")))
    undated_update_days = max(0, int(os.getenv("GOOGLE_CLASSROOM_NO_DUE_UPDATE_DAYS", "30")))
    due = work.get("dueDate", {}) or {}
    try:
        due_day = datetime.date(int(due["year"]), int(due["month"]), int(due["day"]))
    except (KeyError, TypeError, ValueError):
        due_day = None
    if due_day is not None:
        return due_day >= now.date() - datetime.timedelta(days=overdue_grace)

    try:
        updated = datetime.datetime.fromisoformat(work.get("updateTime", "").replace("Z", "+00:00"))
        return updated >= now - datetime.timedelta(days=undated_update_days)
    except (AttributeError, ValueError):
        return False


def _get_active_courses() -> list:
    """Fetch active Canvas courses with caching.
    Returns list of {id, name} dicts. Cache lives 7 days.
    """
    now = time.time()
    try:
        cache = AtomicJSONStore(config.COMPOSIO_CANVAS_CACHE_FILE, dict).read()
        if isinstance(cache, dict):
            if cache.get("timestamp", 0) + CANVAS_CACHE_TTL > now:
                return cache.get("courses", [])
    except (StorageError, OSError, TypeError):
        pass

    r = _call_mcp("CANVAS_LIST_COURSES", {"per_page": 50}, entity_id=ENTITY_CANVAS)
    courses = []
    if r and r.get("successful"):
        raw = r.get("data", {}).get("response_data", [])
        for c in raw:
            cid = c.get("id")
            name = c.get("name")
            if cid and name:
                courses.append({"id": str(cid), "name": name})

    try:
        AtomicJSONStore(config.COMPOSIO_CANVAS_CACHE_FILE, dict).write({"timestamp": now, "courses": courses})
    except (StorageError, OSError):
        pass

    return courses


# ── Gmail ───────────────────────────────────────────────────────────────────

def get_unread_emails(limit: int = 5) -> str:
    """Fetch recent unread emails via Composio Gmail."""
    r = _call_mcp("GMAIL_FETCH_EMAILS", {"max_results": limit})
    if not r or not r.get("successful"):
        return f"Error fetching Gmail via Composio: {r.get('data', {}).get('message', 'unknown')}"

    emails = r.get("data", {}).get("response_data", [])
    if not emails:
        return "No unread emails."

    result = ["📧 **Recent Unread Emails (via Composio):**"]
    for msg in emails:
        subject = msg.get("subject", msg.get("Subject", "No Subject"))
        sender = msg.get("from", msg.get("From", "Unknown Sender"))
        date = msg.get("date", msg.get("Date", ""))
        snippet = msg.get("snippet", "")[:120]
        result.append(f"From: {sender}\nSubject: {subject}\nDate: {date}\n{snippet}\n")

    return "\n".join(result)


# ── Google Classroom ────────────────────────────────────────────────────────

def get_classroom_assignments() -> str:
    """Fetch recent coursework from Google Classroom via Composio."""
    # First list courses
    r = _call_mcp("GOOGLE_CLASSROOM_COURSES_LIST", {})
    if not r or not r.get("successful"):
        return f"Error fetching Classroom courses via Composio: {r.get('data', {}).get('message', 'unknown')}"

    courses_data = r.get("data", {})
    courses = courses_data.get("response_data", courses_data.get("courses", []))
    if not courses:
        return "No active Google Classroom courses found."

    result = ["🏫 **Google Classroom Assignments (via Composio):**"]
    for course in courses:
        course_id = str(course.get("id", ""))
        course_name = course.get("name", "Unknown Course")
        if not course_id:
            continue

        try:
            r2 = _call_mcp("GOOGLE_CLASSROOM_COURSE_WORK_LIST", {
                "courseId": course_id,
                "pageSize": 15
            })
            if r2 and r2.get("successful"):
                cw_data = r2.get("data", {})
                works = cw_data.get("response_data", cw_data.get("courseWork", []))
                for work in works:
                    if not _classroom_work_is_actionable(work):
                        continue
                    title = work.get("title", "Untitled")
                    due_date = work.get("dueDate", {}) or {}
                    due_str = "No due date"
                    if due_date.get("year"):
                        due_str = f"{due_date['year']}-{due_date.get('month',0):02d}-{due_date.get('day',0):02d}"

                    result.append(f"[{course_name}] {title} — Due: {due_str}")
            else:
                logger.warning(f"Could not fetch coursework for {course_name}: {r2.get('data',{}).get('message','') if r2 else 'no response'}")
        except Exception as e:
            logger.warning(f"Error fetching coursework for {course_name}: {e}")

    if len(result) == 1:
        return "No recent published coursework found."

    return "\n".join(result)


def _calendar_task_type(title: str) -> str:
    normalized = title.lower()
    if any(word in normalized for word in ("test", "quiz", "exam")):
        return "Test"
    if "project" in normalized:
        return "Project"
    if any(word in normalized for word in ("reading", "read ")):
        return "Reading"
    return "Assignment"


def get_calendar_assignments() -> list[dict]:
    """Return due-dated Classroom coursework without rendering digest text."""
    response = _call_mcp("GOOGLE_CLASSROOM_COURSES_LIST", {})
    if not response or not response.get("successful"):
        logger.warning("Could not list Classroom courses for calendar sync.")
        return []
    data = response.get("data", {})
    courses = data.get("response_data", data.get("courses", []))
    result: list[dict] = []
    for course in courses or []:
        course_id = str(course.get("id") or "")
        course_name = str(course.get("name") or "Unnamed course")
        if not course_id:
            continue
        response = _call_mcp("GOOGLE_CLASSROOM_COURSE_WORK_LIST", {"courseId": course_id, "pageSize": 100})
        if not response or not response.get("successful"):
            logger.info("Could not fetch Classroom calendar work for %s.", course_name)
            continue
        data = response.get("data", {})
        works = data.get("response_data", data.get("courseWork", []))
        for work in works or []:
            if not _classroom_work_is_actionable(work) or not work.get("dueDate") or not work.get("id"):
                continue
            title = str(work.get("title") or "Untitled")
            result.append({
                "id": f"{course_id}:{work['id']}",
                "title": title,
                "course": course_name,
                "due_date": work.get("dueDate"),
                "url": work.get("alternateLink"),
                "task_type": _calendar_task_type(title),
                "status": "Not started",
                "official": True,
            })
    return result


# Per-announcement body budget.  Announcements carry the scheduling detail the
# digest exists to surface (dates, times, room changes), and those often appear
# late in the text -- "we will be having class on Friday (7/24) instead" sat at
# offset ~180.  The old 300-char cap severed such clauses, so the information
# could never reach a Notion task or a calendar event.  Keep the whole body up to
# a generous ceiling that still bounds a pathological post.
ANNOUNCEMENT_BODY_CHARS = 1200


def get_classroom_announcements(limit: int = 10) -> str:
    """Fetch recent announcements from Google Classroom via Composio."""
    r = _call_mcp("GOOGLE_CLASSROOM_COURSES_LIST", {})
    if not r or not r.get("successful"):
        return f"Error fetching Classroom courses via Composio: {r.get('data', {}).get('message', 'unknown')}"

    courses_data = r.get("data", {})
    courses = courses_data.get("response_data", courses_data.get("courses", []))
    if not courses:
        return "No active Google Classroom courses found."

    result = ["📢 **Google Classroom Announcements (via Composio):**"]
    for course in courses:
        course_id = str(course.get("id", ""))
        course_name = course.get("name", "Unknown Course")
        if not course_id:
            continue

        try:
            r2 = _call_mcp("GOOGLE_CLASSROOM_COURSES_ANNOUNCEMENTS_LIST", {
                "courseId": course_id,
                "announcementStates": ["PUBLISHED"],
                "pageSize": limit
            })
            if r2 and r2.get("successful"):
                ann_data = r2.get("data", {})
                announcements = ann_data.get("response_data", ann_data.get("announcements", []))
                for ann in announcements:
                    text = ann.get("text", ann.get("Text", "")).strip()
                    if text:
                        text = _strip_html(text)
                        # Keep enough of the body that dates, times and room
                        # numbers survive: the 300-char cap used to sever
                        # "class on Friday (7/24)" mid-sentence, so the item
                        # could never become a task or calendar event.
                        if len(text) > ANNOUNCEMENT_BODY_CHARS:
                            text = text[:ANNOUNCEMENT_BODY_CHARS].rstrip() + "…"
                        result.append(f"[{course_name}]: {text}")
        except Exception as e:
            logger.warning(f"Error fetching announcements for {course_name}: {e}")

    if len(result) == 1:
        return "No recent classroom announcements found."

    return "\n".join(result)


# ── Google Docs ─────────────────────────────────────────────────────────────

def _doc_plaintext(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("plain_text", "text", "content", "plaintext"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
    return ""


def _doc_records_from_response(response: dict | None) -> list[dict]:
    data = (response or {}).get("data", {})
    if not isinstance(data, dict):
        return []
    payload = data.get("response_data")
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("files", "documents", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
    for key in ("files", "documents", "items"):
        nested = data.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
    return []


def get_recent_google_doc_records(limit: int = 10) -> list[dict]:
    """Return document metadata and plaintext for calendar deadline extraction."""
    r = _call_mcp("GOOGLEDRIVE_FIND_FILE", {
        "q": "mimeType='application/vnd.google-apps.document' and trashed=false"
    })
    if r and r.get("successful"):
        files = _doc_records_from_response(r)
    else:
        r = _call_mcp("GOOGLEDOCS_SEARCH_DOCUMENTS", {"q": "mimeType='application/vnd.google-apps.document'"})
        if r and r.get("successful"):
            files = _doc_records_from_response(r)
        else:
            logger.warning("Could not list Google Docs for calendar extraction.")
            return []

    records: list[dict] = []
    for doc in files[:max(1, limit)]:
        if not isinstance(doc, dict):
            continue
        doc_id = doc.get("id", doc.get("documentId", ""))
        title = doc.get("name", doc.get("title", doc.get("Name", "Untitled")))
        if not doc_id:
            continue
        r2 = _call_mcp("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", {"document_id": doc_id})
        if r2 and r2.get("successful"):
            response_data = r2.get("data", {})
            content = _doc_plaintext(
                response_data.get("response_data", response_data)
                if isinstance(response_data, dict) else response_data
            )
            records.append({
                "id": str(doc_id),
                "title": str(title),
                "url": str(doc.get("webViewLink") or doc.get("url") or f"https://docs.google.com/document/d/{doc_id}/edit"),
                "content": content[:20_000],
            })
        else:
            logger.info("Could not fetch Google Doc plaintext for calendar extraction.")
    return records


def get_recent_google_docs() -> str:
    """Fetch recently modified Google Docs via Composio."""
    records = get_recent_google_doc_records()
    if not records:
        return "No recently modified Google Docs found."

    output = ["📄 **Recent Google Docs (via Composio):**"]
    for doc in records:
        text_content = str(doc.get("content") or "")
        title = str(doc.get("title") or "Untitled")
        if text_content:
            preview = text_content[:1000] + ("\n...[truncated]" if len(text_content) > 1000 else "")
            output.append(f"--- Doc: {title} ---\n{preview}\n")
        else:
            output.append(f"--- Doc: {title} ---\n(empty content)\n")

    return "\n".join(output)


# ── Google Calendar ─────────────────────────────────────────────────────────

def get_calendar_events(days: int = 3) -> str:
    """Fetch upcoming Google Calendar events via Composio."""
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc)
    time_min = now.isoformat()
    time_max = (now + datetime.timedelta(days=days)).isoformat()
    r = _call_mcp("GOOGLECALENDAR_EVENTS_LIST", {
        "max_results": 20,
        "time_min": time_min,
        "time_max": time_max,
        "order_by": "startTime",
        "single_events": True,
        "calendar_id": "primary"
    })
    if not r or not r.get("successful"):
        return f"Error fetching Calendar via Composio: {r.get('data', {}).get('message', 'unknown') if r else 'no response'}"

    events_data = r.get("data", {})
    events = events_data.get("response_data", events_data.get("items", []))
    if not events:
        return "No upcoming calendar events."

    result = ["📅 **Upcoming Calendar Events (via Composio):**"]
    for ev in events[:15]:
        summary = ev.get("summary", "Untitled Event")
        start = ev.get("start", {})
        start_time = start.get("dateTime", start.get("date", ""))
        result.append(f"- {summary} — {start_time}")

    return "\n".join(result)


# ── Canvas ──────────────────────────────────────────────────────────────────

def get_all_canvas_data() -> str:
    """Fetch assignments, announcements, and pages from Canvas via Composio.
    Iterates over active courses to gather data from each. Course list cached for 7 days."""
    parts = []

    # Get user info
    r = _call_mcp("CANVAS_GET_CURRENT_USER", {}, entity_id=ENTITY_CANVAS)
    user_id = None
    if r and r.get("successful"):
        user_data = r.get("data", {}).get("response_data", {})
        user_id = user_data.get("id")
    if not user_id:
        return "Canvas data unavailable — could not determine user ID."

    courses = _get_active_courses()
    if not courses:
        return "No active Canvas courses found."

    assignments_section = ["📚 **Canvas Assignments (via Composio):**"]
    pages_section = ["📄 **Canvas Pages (via Composio):**"]
    announcements_section = ["📢 **Canvas Announcements (via Composio):**"]
    has_assignments = False
    has_pages = False
    has_announcements = False

    for course in courses:
        course_id = course.get("id")
        course_name = course.get("name")
        if not course_id or not course_name:
            continue
        course_id_str = course_id

        # ── Assignments ──
        r2 = _call_mcp("CANVAS_GET_ALL_ASSIGNMENTS", {"course_id": course_id_str}, entity_id=ENTITY_CANVAS)
        if r2 and r2.get("successful"):
            assignments = r2.get("data", {}).get("response_data", [])
            for a in assignments[:5]:
                name = a.get("name", "Untitled")
                due = a.get("due_at", a.get("dueDate", "No due date"))
                desc = a.get("description", "") or ""
                desc = _strip_html(desc)
                desc_preview = desc[:200] + "..." if len(desc) > 200 else desc
                assignments_section.append(f"- [{course_name}] {name} (Due: {due})")
                if desc_preview:
                    assignments_section.append(f"  {desc_preview}")
                has_assignments = True

        # ── Announcements ──
        context_code = f"course_{course_id}"
        r3 = _call_mcp("CANVAS_LIST_ANNOUNCEMENTS", {
            "context_codes": [context_code],
            "per_page": 5
        }, entity_id=ENTITY_CANVAS)
        if r3 and r3.get("successful"):
            announcements = r3.get("data", {}).get("response_data", [])
            for ann in announcements[:3]:
                title = ann.get("title", "Untitled")
                msg = ann.get("message", "") or ""
                msg = _strip_html(msg)
                msg_preview = msg[:200] + "..." if len(msg) > 200 else msg
                announcements_section.append(f"- [{course_name}] {title}")
                if msg_preview:
                    announcements_section.append(f"  {msg_preview}")
                has_announcements = True

        # ── Pages (first page from each course) ──
        r4 = _call_mcp("CANVAS_LIST_PAGES_FOR_COURSE", {"course_id": course_id_str}, entity_id=ENTITY_CANVAS)
        if r4 and r4.get("successful"):
            pages = r4.get("data", {}).get("response_data", [])
            if pages:
                page = pages[0]
                title = page.get("title", "Untitled")
                url = page.get("html_url", page.get("url", ""))
                pages_section.append(f"- [{course_name}] {title}")
                if url:
                    pages_section[-1] += f"\n    {url}"
                has_pages = True

    if has_assignments:
        parts.append("\n".join(assignments_section))
    if has_announcements:
        parts.append("\n".join(announcements_section))
    if has_pages:
        parts.append("\n".join(pages_section))

    return "\n\n".join(parts) if parts else "No Canvas data available via Composio."


# ── GitHub ──────────────────────────────────────────────────────────────────

def get_github_notifications() -> str:
    """Fetch recent GitHub notifications via Composio."""
    r = _call_mcp("GITHUB_LIST_NOTIFICATIONS", {"per_page": 10}, entity_id="default")
    if not r or not r.get("successful"):
        return ""

    notifications = r.get("data", {}).get("response_data", [])
    if not notifications:
        return ""

    result = ["🐙 **GitHub Notifications (via Composio):**"]
    for n in notifications[:10]:
        repo = n.get("repository", {}).get("full_name", "?")
        title = n.get("subject", {}).get("title", "?")
        url = n.get("subject", {}).get("url", "")
        result.append(f"- [{repo}] {title}")
    return "\n".join(result)
