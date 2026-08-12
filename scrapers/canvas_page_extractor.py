import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# ── Result cache ─────────────────────────────────────────────────────────────
# Canvas course pages are stable for days at a time, but the calendar refresh
# runs every ~30 min against every recently-updated page. Without a cache we
# would re-run local RPC inference on the same unchanged HTML every cycle,
# serially, blocking the calendar path for minutes. Key the cached extraction
# by a hash of (page identity + HTML body): unchanged page => instant return,
# no inference call.
try:
    from config import CACHE_DIR

    _CACHE_PATH = CACHE_DIR / "canvas_page_extractions.json"
except Exception:  # pragma: no cover - config always importable at runtime
    _CACHE_PATH = None

_cache_lock = threading.Lock()

# Per-process wall-clock budget for a single calendar collection pass. Cold
# cache with many new pages must not block the calendar indefinitely; once the
# budget is spent, remaining uncached pages are skipped this cycle and picked up
# next run (their HTML is stable, so they will be cached then).
_RUN_BUDGET_SECONDS = float(os.getenv("CANVAS_EXTRACT_RUN_BUDGET_SECONDS", "90"))
_PER_PAGE_TIMEOUT = float(os.getenv("CANVAS_EXTRACT_PAGE_TIMEOUT_SECONDS", "45"))
_run_state = threading.local()


def reset_extraction_budget() -> None:
    """Start a fresh per-run extraction budget. Call once per calendar pass."""
    _run_state.deadline = time.monotonic() + _RUN_BUDGET_SECONDS


def _budget_remaining() -> float:
    deadline = getattr(_run_state, "deadline", None)
    if deadline is None:
        # No explicit pass started (e.g. direct/manual call): allow one page.
        return _PER_PAGE_TIMEOUT
    return max(0.0, deadline - time.monotonic())


def _load_cache() -> dict:
    if _CACHE_PATH is None:
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    if _CACHE_PATH is None:
        return
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Bound growth: keep the 200 most-recent entries.
        if len(cache) > 200:
            cache = dict(list(cache.items())[-200:])
        _CACHE_PATH.write_text(json.dumps(cache))
    except OSError as exc:
        logger.debug("Could not persist Canvas extraction cache: %s", exc)


def _extract_json_array(raw: str) -> list | None:
    """Best-effort recovery of a JSON array from a model response.

    Handles markdown fences and leading/trailing prose that small local models
    sometimes emit around the payload.
    """
    if not raw:
        return None
    clean = raw.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?", "", clean).strip()
        if clean.endswith("```"):
            clean = clean[:-3].strip()
    try:
        parsed = json.loads(clean)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\[.*\]", clean, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _decorate(items: list, course_id: str, course_name: str, page_url: str) -> list[dict]:
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("title") or not item.get("due_date"):
            continue
        item = dict(item)
        item["course"] = course_name
        id_str = f"{page_url}-{item.get('title')}-{item.get('due_date')}"
        item["id"] = f"page-{hashlib.md5(id_str.encode()).hexdigest()[:12]}"
        item["url"] = f"https://forsyth.instructure.com/courses/{course_id}/pages/{page_url}"
        item["official"] = False  # must be approved as a proposal
        out.append(item)
    return out


def extract_assignments_from_html(
    course_id: str,
    course_name: str,
    page_title: str,
    page_url: str,
    html_body: str,
) -> list[dict]:
    """Extract assignments from Canvas page HTML using the local RPC cluster.

    Canvas page bodies can contain student PII, so inference stays on the local
    cluster (private, no cloud quota) rather than the cloud agy/Gemini CLI. The
    previous agy subprocess path was fragile: it died whenever the Gemini free
    tier hit RESOURCE_EXHAUSTED (429), which took down page extraction for the
    whole calendar. Results are cached by page-content hash and bounded by a
    per-run time budget so a slow or cold pass cannot stall the calendar.
    """
    text = re.sub(r"<[^>]+>", " ", html_body or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    cache_key = hashlib.sha256(
        f"{course_id}|{page_url}|{text}".encode()
    ).hexdigest()[:24]

    # Fast path: unchanged page → return cached extraction, no inference.
    with _cache_lock:
        cache = _load_cache()
        cached = cache.get(cache_key)
    if cached is not None:
        return _decorate(cached, course_id, course_name, page_url)

    # New/changed page. Respect the per-run budget before spending an RPC call.
    remaining = _budget_remaining()
    if remaining <= 1.0:
        logger.info(
            "Canvas page extraction budget exhausted; deferring page '%s' to next cycle",
            page_title,
        )
        return []

    prompt = f"""You are helping a student organize their calendar.
Below is text extracted from a Canvas course page titled '{page_title}' for the course '{course_name}'.
Extract any upcoming tests, quizzes, readings, or assignments along with their due dates.
Assume the current year is {datetime.now().year}.
Respond ONLY with valid JSON in this exact format (no markdown blocks, no extra text):
[
  {{
    "title": "Task Name",
    "due_date": "YYYY-MM-DD",
    "task_type": "Test"
  }}
]
Task types should be one of: Test, Project, Reading, Assignment.
If there are no actionable dates, output ONLY: []

Text:
{text[:8000]}
"""

    raw = ""
    try:
        from llm_router import call_local_rpc

        raw = call_local_rpc(
            prompt=prompt,
            system_prompt="You extract calendar tasks and output ONLY a valid JSON array. No prose.",
            max_tokens=1024,
            temperature=0.0,
            timeout=min(_PER_PAGE_TIMEOUT, max(5.0, remaining)),
        )
    except Exception as exc:  # noqa: BLE001 - inference backend is best-effort
        logger.warning("Canvas page extraction inference failed for %s: %s", page_title, exc)
        return []

    if not raw or not raw.strip():
        logger.info("Canvas page extraction returned no output for %s", page_title)
        return []

    parsed = _extract_json_array(raw)
    if parsed is None:
        logger.warning("Canvas page extraction produced non-JSON output for %s", page_title)
        return []

    # Keep only well-formed rows in the cache (store the raw parsed rows;
    # metadata is re-decorated on read so cache stays identity-agnostic).
    clean_rows = [
        {"title": r["title"], "due_date": r["due_date"], "task_type": r.get("task_type", "Assignment")}
        for r in parsed
        if isinstance(r, dict) and r.get("title") and r.get("due_date")
    ]
    with _cache_lock:
        cache = _load_cache()
        cache[cache_key] = clean_rows
        _save_cache(cache)

    return _decorate(clean_rows, course_id, course_name, page_url)
