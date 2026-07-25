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

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
COMPOSIO_TOKEN_PATH = os.path.expanduser("~/.hermes/mcp-tokens/composio.json")
COMPOSIO_HOST = "connect.composio.dev"
COMPOSIO_PATH = "/mcp"
CANVAS_CACHE_PATH = os.path.expanduser("~/.hermes/canvas_courses_cache.json")
CANVAS_CACHE_TTL = 3600 * 24 * 7  # 1 week — course list rarely changes

# Entity IDs used per app
ENTITY_GOOGLE = "default"          # Google apps use "default" entity
ENTITY_CANVAS = "canvas_ionone-arided"  # Canvas entity ID


# ── Internal helpers ────────────────────────────────────────────────────────

def _load_token() -> Optional[str]:
    """Load Composio access token from the Hermes MCP token store."""
    try:
        with open(COMPOSIO_TOKEN_PATH) as f:
            tokens = json.load(f)
        return tokens.get("access_token")
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        logger.error(f"Failed to load Composio token: {e}")
        return None


def _call_mcp(tool_slug: str, arguments: dict, entity_id: str = ENTITY_GOOGLE) -> Optional[dict]:
    """
    Call a Composio MCP tool via the JSON-RPC endpoint.
    Returns the response data dict (successful: bool, data: {...}).
    """
    token = _load_token()
    if not token:
        return {"successful": False, "data": {"message": "Composio token not available"}}

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

    try:
        conn = http.client.HTTPSConnection(COMPOSIO_HOST, timeout=15)
        conn.request("POST", COMPOSIO_PATH, body=payload, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"
        })
        resp = conn.getresponse()
        text = resp.read().decode()
        conn.close()

        # Parse SSE-style response (data: {...} lines)
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data: "):
                rpc_response = json.loads(line[6:])
                if "result" in rpc_response:
                    for content_item in rpc_response["result"].get("content", []):
                        if content_item.get("type") == "text":
                            inner = json.loads(content_item["text"])
                            results = inner.get("data", {}).get("results", [])
                            if results:
                                return results[0].get("response", {})
                return {"successful": False, "data": {"message": "No result in MCP response"}}

        return {"successful": False, "data": {"message": "Unparseable MCP response"}}
    except Exception as e:
        logger.error(f"Composio MCP call failed ({tool_slug}): {e}")
        return {"successful": False, "data": {"message": f"MCP error: {e}"}}


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
        if os.path.exists(CANVAS_CACHE_PATH):
            with open(CANVAS_CACHE_PATH) as f:
                cache = json.load(f)
            if cache.get("timestamp", 0) + CANVAS_CACHE_TTL > now:
                return cache.get("courses", [])
    except (json.JSONDecodeError, OSError):
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
        os.makedirs(os.path.dirname(CANVAS_CACHE_PATH), exist_ok=True)
        with open(CANVAS_CACHE_PATH, "w") as f:
            json.dump({"timestamp": now, "courses": courses}, f)
    except OSError:
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
                        result.append(f"[{course_name}]: {text[:300]}")
        except Exception as e:
            logger.warning(f"Error fetching announcements for {course_name}: {e}")

    if len(result) == 1:
        return "No recent classroom announcements found."

    return "\n".join(result)


# ── Google Docs ─────────────────────────────────────────────────────────────

def get_recent_google_docs() -> str:
    """Fetch recently modified Google Docs via Composio."""
    # Search for recent docs via Drive
    r = _call_mcp("GOOGLEDRIVE_FIND_FILE", {
        "q": "mimeType='application/vnd.google-apps.document' and trashed=false"
    })
    if r and r.get("successful"):
        files = r.get("data", {}).get("response_data", [])
        if not files:
            return "No recently modified Google Docs found."
    else:
        # Fallback: try the Docs-specific search
        r = _call_mcp("GOOGLEDOCS_SEARCH_DOCUMENTS", {"q": "mimeType='application/vnd.google-apps.document'"})
        if r and r.get("successful"):
            files = r.get("data", {}).get("response_data", [])
            if not files:
                return "No recently modified Google Docs found."
        else:
            return f"Error fetching Google Docs via Composio: {r.get('data', {}).get('message', 'unknown') if r else 'no response'}"

    output = ["📄 **Recent Google Docs (via Composio):**"]
    for doc in files[:10]:
        doc_id = doc.get("id", doc.get("documentId", ""))
        title = doc.get("name", doc.get("title", doc.get("Name", "Untitled")))
        if not doc_id:
            continue

        # Fetch plain text content
        r2 = _call_mcp("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", {"document_id": doc_id})
        if r2 and r2.get("successful"):
            text_content = r2.get("data", {}).get("response_data", "")
            if isinstance(text_content, str) and text_content.strip():
                if len(text_content) > 1000:
                    text_content = text_content[:1000] + "\n...[truncated]"
                output.append(f"--- Doc: {title} ---\n{text_content}\n")
            else:
                output.append(f"--- Doc: {title} ---\n(empty content)\n")
        else:
            output.append(f"--- Doc: {title} ---\n(could not fetch content)\n")

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
