import os
import requests
import logging
import time as _time
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any
import config

logger = logging.getLogger(__name__)

NOTION_API_KEY = config.NOTION_API_KEY
DATABASE_ID = config.NOTION_DATABASE_ID  # Tracker database

# ── Rate Limiter (Notion: 3 req/s) ──────────────────────────────────────────
class _RateLimiter:
    def __init__(self, min_interval=0.35):
        self.min_interval = min_interval
        self.last_call = 0
        self.lock = threading.Lock()

    def wait(self):
        # Calculate sleep time under lock, then sleep outside lock
        with self.lock:
            elapsed = _time.time() - self.last_call
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
            else:
                sleep_time = 0
            self.last_call = _time.time() + sleep_time
        
        # Sleep outside the lock to avoid blocking other threads
        if sleep_time > 0:
            _time.sleep(sleep_time)

_notion_limiter = _RateLimiter()


def _rate_limited_request(method, url, **kwargs):
    """Make a rate-limited HTTP request."""
    _notion_limiter.wait()
    resp = requests.request(method, url, **kwargs)
    # Retry on 429
    if resp.status_code == 429:
        retry_after = float(resp.headers.get("Retry-After", 1.0))
        logger.warning(f"Notion rate limited, waiting {retry_after}s")
        _time.sleep(retry_after)
        _notion_limiter.wait()
        resp = requests.request(method, url, **kwargs)
    return resp
OWNER_ID = "2f9d872b-594c-8115-84a6-00028eb47924"     # Sanel Lathiya

# Schema reference (read-only formula fields, do NOT set these):
#   Progress     → formula (auto: start/end values)
#   Days until due → formula (auto: days from today to due date)

PRIORITY_OPTIONS = {"high", "medium", "low"}
STATUS_OPTIONS   = {"Not started", "In progress", "Done"}
TASK_TYPE_OPTIONS = {"Assignment", "Test", "Project", "Reading", "Other"}

_schema_cache: dict[str, Any] = {"expires_at": 0.0, "properties": {}}
_schema_cache_lock = threading.Lock()


def _notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }


def _parse_due_date(value: Any) -> date | None:
    """Accept a Notion/Canvas ISO date and return its calendar day."""
    if not value or str(value).lower() in {"none", "null", "unknown"}:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def determine_task_priority(
    due_date: str | None,
    suggested_priority: str | None = None,
    *,
    today: date | None = None,
) -> str:
    """Give task priority a consistent, deadline-aware meaning.

    Deadlines win over a model guess: anything due in the next three days (or
    overdue) is high, and the next week is medium.  A model may still mark a
    longer-term major test/project as high.  This avoids a digest silently
    turning every item into the old hard-coded ``Medium`` default.
    """
    suggested = (suggested_priority or "").lower().strip()
    suggested = suggested if suggested in PRIORITY_OPTIONS else None
    due = _parse_due_date(due_date)
    if due is None:
        return suggested or "medium"

    today = today or date.today()
    days_until_due = (due - today).days
    high_days = max(0, int(os.getenv("TASK_HIGH_PRIORITY_DAYS", "3")))
    medium_days = max(high_days, int(os.getenv("TASK_MEDIUM_PRIORITY_DAYS", "7")))

    if days_until_due <= high_days:
        return "high"
    if days_until_due <= medium_days:
        return "high" if suggested == "high" else "medium"
    return "high" if suggested == "high" else (suggested or "low")


def priority_emoji(priority: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🔵"}.get(priority.lower(), "⚪")


def get_task_tracker_schema(*, force_refresh: bool = False) -> dict[str, Any]:
    """Return the Tracker's properties, caching the read for five minutes.

    The base tracker works without the optional task-hub fields.  Looking up
    the schema lets us use those fields automatically after the opt-in upgrade
    without breaking a user's existing database.
    """
    if not NOTION_API_KEY or NOTION_API_KEY == "your_notion_api_key":
        return {}
    now = _time.time()
    with _schema_cache_lock:
        if not force_refresh and _schema_cache["expires_at"] > now:
            return dict(_schema_cache["properties"])

    try:
        response = _rate_limited_request(
            "GET",
            f"https://api.notion.com/v1/databases/{DATABASE_ID}",
            headers=_notion_headers(),
            timeout=15,
        )
        response.raise_for_status()
        properties = response.json().get("properties", {})
        with _schema_cache_lock:
            _schema_cache.update({"expires_at": now + 300, "properties": properties})
        return dict(properties)
    except Exception as exc:
        logger.info("Could not read optional Notion Tracker schema: %s", exc)
        return {}


def _set_optional_property(
    properties: dict[str, Any],
    schema: dict[str, Any],
    name: str,
    value: Any,
) -> None:
    """Set an optional task-hub property only when the existing type fits."""
    if not value or name not in schema:
        return
    property_type = schema[name].get("type")
    text_value = str(value)[:2000]
    if property_type == "select":
        properties[name] = {"select": {"name": text_value}}
    elif property_type == "rich_text":
        properties[name] = {"rich_text": [{"text": {"content": text_value}}]}
    elif property_type == "url":
        properties[name] = {"url": text_value}
    elif property_type == "date":
        properties[name] = {"date": {"start": text_value}}


def task_exists(title: str, headers: dict, fuzzy: bool = True) -> bool:
    """
    Check if a task with this title (or similar) already exists.

    Fast path: exact match via Notion filter (O(1) query).
    Slow path: fuzzy scan of all non-Done tasks (only if no exact match).
    """
    query_url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    norm_title = title.lower().strip()

    # ── Fast path: exact match ──────────────────────────────────────────
    exact_payload = {
        "page_size": 1,
        "filter": {
            "and": [
                {"property": "Task name", "title": {"equals": title}},
                {"property": "Status", "status": {"does_not_equal": "Done"}},
            ]
        }
    }
    try:
        res = _rate_limited_request("POST", query_url, headers=headers, json=exact_payload, timeout=10)
        res.raise_for_status()
        if len(res.json().get("results", [])) > 0:
            logger.info(f"Task '{title}' already exists (exact match). Skipping.")
            return True
    except Exception as e:
        logger.error(f"Notion exact-match query failed: {e}")

    # ── Slow path: fuzzy scan of all non-Done tasks ─────────────────────
    if not fuzzy:
        return False

    fuzzy_payload = {
        "page_size": 100,
        "filter": {"property": "Status", "status": {"does_not_equal": "Done"}}
    }
    try:
        res = _rate_limited_request("POST", query_url, headers=headers, json=fuzzy_payload, timeout=10)
        res.raise_for_status()
        results = res.json().get("results", [])

        import difflib
        for r in results:
            title_props = r.get("properties", {}).get("Task name", {}).get("title", [])
            existing = title_props[0].get("text", {}).get("content", "") if title_props else ""
            existing_norm = existing.lower().strip()

            similarity = difflib.SequenceMatcher(None, norm_title, existing_norm).ratio()
            if similarity > 0.75:
                logger.info(
                    f"Task '{title}' is {similarity:.0%} similar to existing "
                    f"'{existing}' — skipping as duplicate."
                )
                return True

        return False
    except Exception as e:
        logger.error(f"Notion fuzzy-dedup query failed: {e}")
        return False


def add_task_to_notion(
    title: str,
    source: str = None,
    due_date: str = None,
    url: str = None,
    course: str = None,
    task_type: str = None,
    priority: str = "medium",
    status: str = "Not started",
    start_value: float = None,
    end_value: float = None,
):
    """Push a new task row to the Tracker database."""
    if not NOTION_API_KEY or NOTION_API_KEY == "your_notion_api_key":
        logger.error("Notion API key not configured.")
        return False

    headers = _notion_headers()

    if task_exists(title, headers):
        logger.info(f"Task '{title}' already exists in Notion. Skipping.")
        return True

    # Normalize priority from a deadline plus the AI's task context.
    priority = determine_task_priority(due_date, priority)

    # Normalize status
    if status not in STATUS_OPTIONS:
        status = "Not started"

    properties = {
        # ── Required ──────────────────────────────────────────────
        "Task name": {
            "title": [{"text": {"content": title}}]
        },
        # ── Owner: always assigned to Sanel ───────────────────────
        "Owner": {
            "people": [{"object": "user", "id": OWNER_ID}]
        },
        # ── Status ────────────────────────────────────────────────
        "Status": {
            "status": {"name": status}
        },
        # ── Priority ──────────────────────────────────────────────
        "Priority": {
            "select": {"name": priority.capitalize()}
        },
    }

    # ── Due date (ISO 8601: "2026-06-25") ─────────────────────────
    if due_date and str(due_date).lower() not in ("null", "none", ""):
        if _parse_due_date(due_date):
            properties["Due date"] = {"date": {"start": str(due_date)}}
        else:
            logger.warning("Bad due_date format '%s'; omitting it from Notion.", due_date)

    # ── Start / End values (optional numbers for progress tracking) ─
    if start_value is not None:
        try:
            properties["Start value"] = {"number": float(start_value)}
        except Exception:
            pass

    if end_value is not None:
        try:
            properties["End value"] = {"number": float(end_value)}
        except Exception:
            pass

    # Optional task-hub metadata is only sent after the schema upgrade has
    # created compatible properties.  Existing simple trackers remain valid.
    schema = get_task_tracker_schema()
    normalized_type = (task_type or "").strip().title()
    if normalized_type not in TASK_TYPE_OPTIONS:
        normalized_type = "Assignment" if source and source.lower() in {"canvas", "google classroom", "classroom"} else "Other"
    _set_optional_property(properties, schema, "Source", source)
    _set_optional_property(properties, schema, "Course", course)
    _set_optional_property(properties, schema, "Task type", normalized_type)
    _set_optional_property(properties, schema, "Link", url)
    _set_optional_property(properties, schema, "Last synced", datetime.now(timezone.utc).date().isoformat())

    data = {
        "parent": {"database_id": DATABASE_ID},
        "properties": properties,
    }

    # Attach source URL as page content if provided
    children = []
    if source or course or url:
        lines = []
        if source:
            lines.append(f"Source: {source}")
        if course:
            lines.append(f"Course: {course}")
        if url:
            lines.append(f"Open task: {url}")
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": "\n".join(lines)}}]
            }
        })
    if children:
        data["children"] = children

    try:
        res = _rate_limited_request(
            "POST",
            "https://api.notion.com/v1/pages",
            headers=headers,
            json=data,
            timeout=15,
        )
        res.raise_for_status()
        page_id = res.json().get("id")
        logger.info(f"Pushed to Notion Tracker: {title} (ID: {page_id})")
        return page_id
    except Exception as e:
        logger.error(f"Failed to push to Notion: {e}")
        if "res" in locals():
            logger.error(res.text[:500])
        return None

def update_notion_task(page_id: str, priority: str = None, status: str = None, start_value: float = None, end_value: float = None):
    """Updates an existing Notion task's priority, status, or progress values."""
    if not NOTION_API_KEY or NOTION_API_KEY == "your_notion_api_key":
        return False
        
    headers = _notion_headers()
    
    properties = {}
    if priority:
        priority = priority.lower()
        if priority in PRIORITY_OPTIONS:
            properties["Priority"] = {"select": {"name": priority.capitalize()}}
            
    if status:
        if status in STATUS_OPTIONS:
            properties["Status"] = {"status": {"name": status}}
            
    if start_value is not None:
        properties["Start value"] = {"number": float(start_value)}
        
    if end_value is not None:
        properties["End value"] = {"number": float(end_value)}
        
    if not properties:
        return True
        
    data = {"properties": properties}
    
    try:
        res = _rate_limited_request("PATCH",
            f"https://api.notion.com/v1/pages/{page_id}",
            headers=headers,
            json=data,
            timeout=15,
        )
        res.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Failed to update Notion page {page_id}: {e}")
        return False


def find_stale_tasks(max_age_days: int = 60, overdue_days: int = 7) -> list[dict[str, str]]:
    """List unfinished tasks that should leave the active queue.

    A real due date takes precedence over the Notion page creation date.  This
    catches an assignment imported today even if it was actually due months
    ago.  Undated tasks retain the conservative age-based fallback.
    """
    if not NOTION_API_KEY or NOTION_API_KEY == "your_notion_api_key":
        logger.error("Notion API key not configured.")
        return []

    headers = _notion_headers()
    query_url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    today = datetime.now(timezone.utc).date()
    due_cutoff = today - timedelta(days=max(0, overdue_days))
    created_cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, max_age_days))
    payload = {
        "page_size": 100,
        "filter": {"property": "Status", "status": {"equals": "Not started"}},
    }
    stale: list[dict[str, str]] = []
    start_cursor = None

    while True:
        page_payload = dict(payload)
        if start_cursor:
            page_payload["start_cursor"] = start_cursor
        try:
            response = _rate_limited_request("POST", query_url, headers=headers, json=page_payload, timeout=15)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.error("Failed to query Notion for stale tasks: %s", exc)
            return stale

        for task in data.get("results", []):
            properties = task.get("properties", {})
            title_parts = properties.get("Task name", {}).get("title", [])
            title = title_parts[0].get("plain_text", "unknown") if title_parts else "unknown"
            due_value = properties.get("Due date", {}).get("date") or {}
            due = _parse_due_date(due_value.get("start"))
            reason = None

            if due is not None and due < due_cutoff:
                reason = f"due {due.isoformat()} ({(today - due).days}d overdue)"
            elif due is None:
                created_text = task.get("created_time", "")
                try:
                    created = datetime.fromisoformat(created_text.replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    created = None
                if created is not None and created < created_cutoff:
                    reason = f"undated and created {(today - created.date()).days}d ago"

            if reason:
                stale.append({"id": task["id"], "title": title, "reason": reason})

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")
        if not start_cursor:
            break

    return stale


def archive_stale_tasks(
    dry_run: bool = False,
    max_age_days: int = 60,
    overdue_days: int = 7,
) -> int:
    """Mark stale tasks Done; never delete or archive their Notion pages."""
    stale_tasks = find_stale_tasks(max_age_days=max_age_days, overdue_days=overdue_days)
    if dry_run:
        for task in stale_tasks:
            logger.info("[DRY RUN] Would mark Done: '%s' (%s)", task["title"], task["reason"])
        logger.info("Cleanup preview complete: %d task(s) would be marked Done.", len(stale_tasks))
        return len(stale_tasks)

    headers = _notion_headers()
    completed = 0
    for task in stale_tasks:
        try:
            response = _rate_limited_request(
                "PATCH",
                f"https://api.notion.com/v1/pages/{task['id']}",
                headers=headers,
                json={"properties": {"Status": {"status": {"name": "Done"}}}},
                timeout=15,
            )
            response.raise_for_status()
            logger.info("Marked stale task Done: '%s' (%s)", task["title"], task["reason"])
            completed += 1
        except Exception as exc:
            logger.error("Failed to mark '%s' Done: %s", task["title"], exc)

    logger.info("Cleanup complete: %d task(s) marked Done.", completed)
    return completed


TASK_HUB_PROPERTIES: dict[str, dict[str, Any]] = {
    "Source": {
        "select": {
            "options": [
                {"name": "Canvas", "color": "blue"},
                {"name": "Google Classroom", "color": "green"},
                {"name": "Gmail", "color": "yellow"},
                {"name": "GroupMe", "color": "purple"},
                {"name": "Manual", "color": "gray"},
            ]
        }
    },
    "Course": {"rich_text": {}},
    "Task type": {
        "select": {
            "options": [
                {"name": "Assignment", "color": "blue"},
                {"name": "Test", "color": "red"},
                {"name": "Project", "color": "orange"},
                {"name": "Reading", "color": "green"},
                {"name": "Other", "color": "gray"},
            ]
        }
    },
    "Link": {"url": {}},
    "Last synced": {"date": {}},
}


def upgrade_task_tracker_schema(dry_run: bool = True) -> list[str]:
    """Add optional task-hub fields without changing existing task data.

    The public Notion API cannot safely define workspace-specific filtered
    views, so this upgrades the data model only.  The companion script prints
    the recommended views to create once in the Notion UI.
    """
    schema = get_task_tracker_schema(force_refresh=True)
    if not schema:
        return []
    missing = {name: definition for name, definition in TASK_HUB_PROPERTIES.items() if name not in schema}
    if not missing:
        logger.info("Notion Tracker already has all task-hub properties.")
        return []
    if dry_run:
        logger.info("[DRY RUN] Would add Notion task-hub properties: %s", ", ".join(missing))
        return list(missing)

    try:
        response = _rate_limited_request(
            "PATCH",
            f"https://api.notion.com/v1/databases/{DATABASE_ID}",
            headers=_notion_headers(),
            json={"properties": missing},
            timeout=15,
        )
        response.raise_for_status()
        with _schema_cache_lock:
            _schema_cache.update({"expires_at": 0.0, "properties": {}})
        logger.info("Added Notion task-hub properties: %s", ", ".join(missing))
        return list(missing)
    except Exception as exc:
        logger.error("Could not upgrade Notion Tracker schema: %s", exc)
        return []


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "archive":
        # Safe by default: the live mutation needs an explicit --apply.
        dry = "--apply" not in sys.argv
        count = archive_stale_tasks(dry_run=dry)
        print(f"{'Would mark Done' if dry else 'Marked Done'} {count} stale tasks.")
    elif len(sys.argv) > 1 and sys.argv[1] == "upgrade":
        dry = "--apply" not in sys.argv
        changed = upgrade_task_tracker_schema(dry_run=dry)
        action = "Would add" if dry else "Added"
        print(f"{action} task-hub properties: {', '.join(changed) if changed else 'none'}")
    else:
        print("Testing Notion Tracker push...")
        success = add_task_to_notion(
            title="Test Task from Bot",
            source="Canvas",
            due_date="2026-06-30",
            priority="high",
            status="Not started",
            start_value=0,
            end_value=100,
        )
        print("✅ Success! Check your Notion Tracker." if success else "❌ Failed.")
