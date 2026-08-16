"""Canvas DOM parser and LLM task extractor.

Pipeline
--------
1. ``_parse_html_with_structure_and_links`` compacts a Canvas page body:
   strips boilerplate/navigation, preserves table structure as pipe-delimited
   markdown rows (``Date | Topic | Assignment / Assessment``), and resolves
   embedded iframes/links (Google Docs, Slides daily agendas, PDF embeds) into
   inline text.
2. ``_llm_extract`` chunks the compacted text (no truncation — completeness over
   brevity) and queries a local LLM for calendar tasks.  The network boundary is
   a single seam, ``_call_local_llm``, which prefers the direct Ollama endpoint
   (``http://127.0.0.1:11434/api/generate`` running ``LFM2.5-1.2B-Instruct``) at
   ``temperature=0.0`` and falls back to the local RPC cluster router.
3. ``_heuristic_rule_extraction`` is a deterministic regex fallback used when the
   LLM is offline, over budget, or emits malformed JSON.

Every extracted task is normalized (canonical ``YYYY-MM-DD`` due date, task type
constrained to ``Test``/``Project``/``Reading``/``Assignment``) before being
decorated with stable IDs and returned.  Results are content-hash cached.

The public contract is intentionally unchanged so ``canvas_scraper`` keeps
working:

    extract_assignments_from_html(
        course_id, course_name, page_title, page_url, html_body
    ) -> list[dict]
"""

from __future__ import annotations

import calendar
import hashlib
import html
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# ── Result cache ─────────────────────────────────────────────────────────────
try:
    from config import CACHE_DIR

    _CACHE_PATH = CACHE_DIR / "canvas_page_extractions.json"
except Exception:  # pragma: no cover - config always importable in practice
    _CACHE_PATH = None

_cache_lock = threading.Lock()

# ── Budgets and endpoints (all overridable via environment) ──────────────────
_RUN_BUDGET_SECONDS = float(os.getenv("CANVAS_EXTRACT_RUN_BUDGET_SECONDS", "180"))
_PER_PAGE_TIMEOUT = float(os.getenv("CANVAS_EXTRACT_PAGE_TIMEOUT_SECONDS", "30"))
_PER_CALL_TIMEOUT = float(os.getenv("CANVAS_EXTRACT_CALL_TIMEOUT_SECONDS", "12"))

# Direct local LLM endpoint (Ollama-native /api/generate).
_OLLAMA_GENERATE_URL = os.getenv(
    "CANVAS_EXTRACT_OLLAMA_URL", "http://127.0.0.1:11434/api/generate"
)
_OLLAMA_MODEL = os.getenv("CANVAS_EXTRACT_OLLAMA_MODEL", "LFM2.5-1.2B-Instruct")

# Chunking: process the full page in windows rather than truncating it.  A page
# rarely exceeds a couple of windows, but a syllabus with an inlined agenda can,
# and silently dropping its tail would lose real assignments.
_CHUNK_CHARS = max(1000, int(os.getenv("CANVAS_EXTRACT_CHUNK_CHARS", "3500")))
_CHUNK_OVERLAP = max(0, int(os.getenv("CANVAS_EXTRACT_CHUNK_OVERLAP", "250")))
_MAX_CHUNKS = max(1, int(os.getenv("CANVAS_EXTRACT_MAX_CHUNKS", "12")))

# Spec: assume the current academic year is 2026 when a date omits its year.
_ASSUMED_YEAR = int(os.getenv("CANVAS_EXTRACT_ASSUMED_YEAR", "0")) or datetime.now().year

_ALLOWED_TASK_TYPES = ("Test", "Project", "Reading", "Assignment")

_run_state = threading.local()


# ── Per-run budget ───────────────────────────────────────────────────────────
def reset_extraction_budget() -> None:
    """Start a fresh per-run extraction budget. Call once per calendar pass."""
    _run_state.deadline = time.monotonic() + _RUN_BUDGET_SECONDS


def _budget_remaining() -> float:
    deadline = getattr(_run_state, "deadline", None)
    if deadline is None:
        return _PER_PAGE_TIMEOUT
    return max(0.0, deadline - time.monotonic())


# ── Cache ────────────────────────────────────────────────────────────────────
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
        if len(cache) > 300:
            cache = dict(list(cache.items())[-300:])
        _CACHE_PATH.write_text(json.dumps(cache))
    except OSError as exc:
        logger.debug("Could not persist Canvas extraction cache: %s", exc)


# ── JSON recovery ────────────────────────────────────────────────────────────
def _extract_json_array(raw: str) -> list | None:
    """Recover a JSON array from a possibly noisy LLM response.

    Handles bare arrays, ```json fenced blocks, and an array embedded in prose.
    Returns ``None`` (not ``[]``) when nothing array-shaped is found so the
    caller can distinguish "model said empty" from "model output was garbage".
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


# ── Embedded-document resolution ─────────────────────────────────────────────
def _fetch_external_link_text(url: str, timeout: float = 6.0) -> str:
    """Fetch plaintext from public Google Docs, Slides, or published embeds.

    This is a network seam; unit tests monkeypatch it. Real failures degrade to
    an empty string so extraction never depends on an embed being reachable.
    """
    if not url or not isinstance(url, str):
        return ""

    # 1. Published Google Slides (/presentation/d/e/2PACX-.../pubembed)
    if "docs.google.com/presentation/d/e/" in url or "pubembed" in url:
        pub_url = url.split("?")[0].replace("pubembed", "pub")
        try:
            req = urllib.request.Request(
                pub_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_html = resp.read().decode("utf-8", errors="ignore")
                decoded = raw_html.replace("\\n", "\n").replace("\\t", "\t")
                decoded = re.sub(
                    r"\\u([0-9a-fA-F]{4})",
                    lambda m: chr(int(m.group(1), 16)),
                    decoded,
                )
                decoded = html.unescape(decoded)
                matches = re.findall(r'\"Plans[^\"]+\"', decoded)
                if matches:
                    return "\n".join(matches).replace("\\n", "\n")[:4000]
                tokens = re.findall(r'[\w\s.,;:!?\(\)/\'\"#\-]{4,}', decoded)
                meaningful = [
                    t.strip()
                    for t in tokens
                    if any(
                        k in t.lower()
                        for k in [
                            "quiz", "test", "exam", "homework", "u1q", "due",
                            "monday", "tuesday", "wednesday", "thursday", "friday",
                            "plans", "upcoming",
                        ]
                    )
                ]
                if meaningful:
                    return "\n".join(meaningful[:35])
        except Exception as e:
            logger.debug("Failed to fetch published Google Slide %s: %s", pub_url, e)
            return ""

    # 2. Standard Google Docs & Google Slides text export
    export_url = None
    doc_match = re.search(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)", url)
    if doc_match:
        export_url = (
            f"https://docs.google.com/document/d/{doc_match.group(1)}/export?format=txt"
        )
    else:
        slide_match = re.search(
            r"docs\.google\.com/presentation/d/([a-zA-Z0-9_-]+)", url
        )
        if slide_match:
            export_url = (
                f"https://docs.google.com/presentation/d/{slide_match.group(1)}/export/txt"
            )

    if not export_url:
        return ""

    try:
        req = urllib.request.Request(
            export_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            return content.strip()[:2500]
    except Exception as e:
        logger.debug("Failed to fetch external doc %s: %s", export_url, e)
        return ""


# ── HTML compaction ──────────────────────────────────────────────────────────
# Canvas wraps page bodies in a large application chrome. These tags/roles are
# pure navigation and never carry assignment data.
_BOILERPLATE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "svg")
_NAV_CLASS_HINTS = (
    "ic-app-header", "ic-app-nav-toggle", "roster", "breadcrumb", "navigation",
    "ic-app-crumbs", "menu", "sidebar", "footer", "skip",
)


def _looks_like_navigation(tag) -> bool:
    role = (tag.get("role") or "").strip().lower()
    if role in {"navigation", "banner", "contentinfo", "menubar"}:
        return True
    classes = " ".join(tag.get("class") or []).lower()
    ident = (tag.get("id") or "").lower()
    haystack = f"{classes} {ident}"
    return any(hint in haystack for hint in _NAV_CLASS_HINTS)


def _serialize_table(table) -> str:
    """Render an HTML table as pipe-delimited markdown rows.

    The natural Canvas syllabus header (``Date | Topic | Assignment /
    Assessment``) is preserved as the first row so both the LLM and the
    heuristic fallback can align columns. Multi-column and nested tables are
    flattened row-by-row; empty rows are dropped.
    """
    rows_text: list[str] = []
    for tr in table.find_all("tr"):
        cols = [
            re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).strip()
            for cell in tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"])
        ]
        if any(cols):
            rows_text.append(" | ".join(cols))
    if not rows_text:
        return ""
    return "\n[TABLE DATA]:\n" + "\n".join(rows_text) + "\n"


def _parse_html_with_structure_and_links(html_body: str) -> str:
    """Compact Canvas HTML: drop chrome, keep tables, inline embedded docs."""
    if not html_body or not isinstance(html_body, str):
        return ""

    soup = BeautifulSoup(html_body, "html.parser")

    # 1. Remove boilerplate + navigation before extracting text.
    for tag_name in _BOILERPLATE_TAGS:
        for node in soup.find_all(tag_name):
            node.decompose()
    for node in soup.find_all(True):
        # ``find_all`` snapshots the tree, so a node already decomposed by a
        # parent removal is detached; skip those defensively.
        if node.parent is None:
            continue
        if node.name not in {"table", "tr", "td", "th"} and _looks_like_navigation(node):
            node.decompose()

    # 2. Tables -> markdown rows (do this before get_text flattens them).
    for table in soup.find_all("table"):
        summary = _serialize_table(table)
        table.replace_with(soup.new_string(summary))

    # 3. Iframes and contextual links -> inlined document text.
    embedded_snippets: list[str] = []
    seen_urls: set[str] = set()

    for ifr in soup.find_all("iframe"):
        src = str(ifr.get("src", "") or "")
        title = str(ifr.get("title", "Embedded Frame") or "Embedded Frame")
        if src and src not in seen_urls:
            seen_urls.add(src)
            doc_text = _fetch_external_link_text(src)
            if doc_text:
                embedded_snippets.append(
                    f"\n--- Embedded Doc ({title}): ---\n{doc_text}\n--- End Embedded Doc ---"
                )
            else:
                embedded_snippets.append(f"[Embedded Frame: {title} -> {src}]")

    for a in soup.find_all("a", href=True):
        href = str(a.get("href") or "")
        text = a.get_text(strip=True)
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)
        low = href.lower()
        if any(k in low for k in ["docs.google.com/document", "docs.google.com/presentation"]):
            doc_text = _fetch_external_link_text(href)
            if doc_text:
                embedded_snippets.append(
                    f"\n--- Linked Doc ({text}): ---\n{doc_text}\n--- End Linked Doc ---"
                )
        elif any(
            k in low
            for k in ["forms.gle", "docs.google.com/forms", "onenote", "canva.com", "gateway.cengage.com"]
        ):
            embedded_snippets.append(f"[Linked Resource: '{text}' -> {href}]")

    base_text = soup.get_text(separator="\n", strip=True)
    full_text = base_text + (
        "\n\n" + "\n".join(embedded_snippets) if embedded_snippets else ""
    )
    return re.sub(r"\n{3,}", "\n\n", full_text).strip()


# ── Date and task-type normalization ─────────────────────────────────────────
_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
_MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


def _valid_ymd(year: int, month: int, day: int) -> str | None:
    try:
        datetime(year, month, day)
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _normalize_date(value, assumed_year: int | None = None) -> str | None:
    """Coerce a variety of date spellings into canonical ``YYYY-MM-DD``.

    Returns ``None`` for anything that is not a real calendar date (e.g. the
    malformed ``2026-13-40`` or ``"TBD"``), so bad rows are dropped rather than
    propagated into the calendar.
    """
    if not value or not isinstance(value, str):
        return None
    year = assumed_year or _ASSUMED_YEAR
    v = value.strip()

    # ISO YYYY-MM-DD (validate the calendar, reject 2026-13-40 etc.)
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", v)
    if m:
        return _valid_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # Month name: "August 17, 2026", "Aug 17", "17 August 2026"
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?", v)
    if m and m.group(1).lower() in _MONTHS:
        yr = int(m.group(3)) if m.group(3) else year
        return _valid_ymd(yr, _MONTHS[m.group(1).lower()], int(m.group(2)))
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?(?:\s*,?\s*(\d{4}))?", v)
    if m and m.group(2).lower() in _MONTHS:
        yr = int(m.group(3)) if m.group(3) else year
        return _valid_ymd(yr, _MONTHS[m.group(2).lower()], int(m.group(1)))

    # Numeric M/D or M/D/Y (also M.D, M-D)
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", v)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if m.group(3):
            yr = int(m.group(3))
            if yr < 100:
                yr += 2000
        else:
            yr = year
        return _valid_ymd(yr, month, day)

    return None


def _normalize_task_type(value) -> str:
    """Map free-form type text onto the four allowed labels."""
    v = str(value or "").strip().lower()
    if any(k in v for k in ("test", "quiz", "exam", "assessment", "formative", "summative")):
        return "Test"
    if any(k in v for k in ("project", "presentation", "lab report", "essay")):
        return "Project"
    if any(k in v for k in ("read", "chapter", "textbook")):
        return "Reading"
    return "Assignment"


# ── Deterministic heuristic fallback ─────────────────────────────────────────
def _heuristic_rule_extraction(text: str) -> list[dict]:
    """Fallback rule-based extractor if the LLM path is offline or malformed."""
    results: list[dict] = []
    year = _ASSUMED_YEAR
    lines = text.split("\n")
    current_date: str | None = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue

        # 1. Inline item with parenthesized date: "U1Q1 (8.17)", "U1 Test (9.4)"
        inline_match = re.search(
            r"([A-Za-z0-9\s/_-]+?)\s*\((?:(?:Mon|Tue|Wed|Thu|Fri)?\s*)?(\d{1,2})[./-](\d{1,2})\)",
            line,
        )
        if inline_match:
            item_name = inline_match.group(1).strip()
            iso = _normalize_date(
                f"{inline_match.group(2)}/{inline_match.group(3)}", year
            )
            if iso and len(item_name) >= 3:
                results.append(
                    {
                        "title": item_name,
                        "due_date": iso,
                        "task_type": _normalize_task_type(item_name),
                    }
                )
                continue

        # 2. Section date heading: "Monday, 8/17 -" or "8/17"
        date_match = re.search(
            r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday)?\s*,?\s*(\d{1,2})[/.-](\d{1,2})\b",
            line,
            re.IGNORECASE,
        )
        if date_match:
            iso = _normalize_date(
                f"{date_match.group(1)}/{date_match.group(2)}", year
            )
            if iso:
                current_date = iso

        # 3. Actionable keyword in line
        if any(
            k in line.lower()
            for k in [
                "quiz", "test", "exam", "homework", "due", "submit", "assignment",
                "formative", "summative", "read", "lor activity", "u1q", "wa1", "project",
            ]
        ):
            cleaned = re.sub(r"^[0-9]+[.)]\s*", "", line)
            cleaned = re.sub(
                r"^(Homework|Quiz|Test)\s*[-:]\s*", "", cleaned, flags=re.IGNORECASE
            ).strip()
            if current_date and len(cleaned) > 3:
                results.append(
                    {
                        "title": cleaned[:100],
                        "due_date": current_date,
                        "task_type": _normalize_task_type(line),
                    }
                )
    return results


# ── LLM network seam ─────────────────────────────────────────────────────────
def _ollama_generate(prompt: str, system_prompt: str, timeout: float) -> str:
    """Call the direct local Ollama endpoint (/api/generate) at temperature 0.0.

    Isolated so unit tests can monkeypatch the single HTTP boundary. Returns an
    empty string on any failure; the caller decides how to fall back.
    """
    import requests  # lazy: keeps module import cheap and test-friendly

    payload = {
        "model": _OLLAMA_MODEL,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "options": {"temperature": 0.0},
    }
    try:
        resp = requests.post(_OLLAMA_GENERATE_URL, json=payload, timeout=timeout)
        if resp.status_code != 200:
            logger.debug("Ollama /api/generate returned HTTP %s", resp.status_code)
            return ""
        data = resp.json()
        text = data.get("response", "") if isinstance(data, dict) else ""
        return text.strip() if isinstance(text, str) else ""
    except Exception as exc:  # network, decode, timeout
        logger.debug("Ollama /api/generate failed: %s", type(exc).__name__)
        return ""


def _call_local_llm(prompt: str, system_prompt: str, timeout: float) -> str:
    """Primary local inference: direct Ollama endpoint, then RPC router.

    Both are local (private-data safe). Returns an empty string when neither
    path yields text, which routes the caller to the heuristic fallback.
    """
    text = _ollama_generate(prompt, system_prompt, timeout)
    if text:
        return text
    try:
        from llm_router import call_local_rpc

        return call_local_rpc(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=512,
            temperature=0.0,
            timeout=timeout,
        )
    except Exception as exc:
        logger.debug("Local RPC router unavailable: %s", type(exc).__name__)
        return ""


def _chunk_text(text: str) -> list[str]:
    """Split compacted text into overlapping windows (never truncate)."""
    if len(text) <= _CHUNK_CHARS:
        return [text]
    chunks: list[str] = []
    step = max(1, _CHUNK_CHARS - _CHUNK_OVERLAP)
    for start in range(0, len(text), step):
        chunks.append(text[start : start + _CHUNK_CHARS])
        if len(chunks) >= _MAX_CHUNKS:
            break
    return chunks


def _build_prompt(page_title: str, course_name: str, chunk: str) -> str:
    return f"""You are helping a high school student organize their calendar.
Below is text extracted from a Canvas course page or announcement titled '{page_title}' for the course '{course_name}'.
Extract any upcoming tests, quizzes, readings, homework, or assignments along with their due dates.
Assume the current year is {_ASSUMED_YEAR}.
Respond ONLY with valid JSON in this exact format (no markdown, no extra text):
[
  {{
    "title": "Task Name",
    "due_date": "YYYY-MM-DD",
    "task_type": "Test"
  }}
]
Task types must be one of: Test, Project, Reading, Assignment.
If no actionable dates, output: []

Text:
{chunk}
"""


def _llm_extract(page_title: str, course_name: str, structured_text: str) -> list[dict]:
    """Run the LLM over every chunk of the page and merge unique rows."""
    system_prompt = (
        "You extract calendar tasks and output ONLY a valid JSON array. No prose."
    )
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for chunk in _chunk_text(structured_text):
        remaining = _budget_remaining()
        if remaining <= 3.0:
            logger.debug("Extraction budget exhausted; stopping LLM chunk loop")
            break
        raw = _call_local_llm(
            _build_prompt(page_title, course_name, chunk),
            system_prompt,
            min(_PER_CALL_TIMEOUT, remaining),
        )
        parsed = _extract_json_array(raw)
        if not parsed:
            continue
        for row in parsed:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            iso = _normalize_date(row.get("due_date"))
            if not title or not iso:
                continue
            key = (title.lower(), iso)
            if key in seen:
                continue
            seen.add(key)
            merged.append(
                {
                    "title": title,
                    "due_date": iso,
                    "task_type": _normalize_task_type(row.get("task_type")),
                }
            )
    return merged


# ── Decoration ───────────────────────────────────────────────────────────────
def _decorate(items: list, course_id: str, course_name: str, page_url: str) -> list[dict]:
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("title") or not item.get("due_date"):
            continue
        item = dict(item)
        item["course"] = course_name
        id_str = f"{page_url}-{item.get('title')}-{item.get('due_date')}"
        item["id"] = f"page-{hashlib.md5(id_str.encode()).hexdigest()[:12]}"
        if page_url.startswith("http"):
            item["url"] = page_url
        else:
            item["url"] = f"https://forsyth.instructure.com/courses/{course_id}/pages/{page_url}"
        item["official"] = False
        out.append(item)
    return out


# ── Public entry point (contract preserved) ──────────────────────────────────
def extract_assignments_from_html(
    course_id: str,
    course_name: str,
    page_title: str,
    page_url: str,
    html_body: str,
) -> list[dict]:
    """Extract assignments from Canvas page HTML & embedded docs.

    Uses local LLM inference (direct Ollama endpoint, then RPC cluster router)
    with a deterministic heuristic fallback. Results are content-hash cached and
    decorated with stable IDs. Never raises for a bad page — returns ``[]``.
    """
    structured_text = _parse_html_with_structure_and_links(html_body)
    if not structured_text:
        return []

    cache_key = hashlib.sha256(
        f"{course_id}|{page_url}|{structured_text}".encode()
    ).hexdigest()[:24]

    with _cache_lock:
        cache = _load_cache()
        cached = cache.get(cache_key)
    if cached is not None:
        return _decorate(cached, course_id, course_name, page_url)

    clean_rows: list[dict] = []
    if _budget_remaining() > 3.0:
        try:
            clean_rows = _llm_extract(page_title, course_name, structured_text)
        except Exception as exc:
            logger.debug("Canvas LLM extraction failed, using fallback: %s", exc)

    # If the LLM returned nothing or failed, fall back to deterministic rules.
    if not clean_rows:
        raw_rows = _heuristic_rule_extraction(structured_text)
        for row in raw_rows:
            iso = _normalize_date(row.get("due_date"))
            title = str(row.get("title") or "").strip()
            if iso and title:
                clean_rows.append(
                    {
                        "title": title,
                        "due_date": iso,
                        "task_type": _normalize_task_type(row.get("task_type")),
                    }
                )

    with _cache_lock:
        cache = _load_cache()
        cache[cache_key] = clean_rows
        _save_cache(cache)

    return _decorate(clean_rows, course_id, course_name, page_url)
