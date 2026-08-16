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
from typing import Callable

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
# Total wall-clock budget for one calendar pass's LLM extraction, shared across
# ALL courses. Sized for a multi-course crawl: ~7 courses x up to 6 pages x ~3s
# warm (1.2B) plus a one-time cold load leaves headroom. Raise it if you add the
# slower 2.6B model or more favorite courses.
_RUN_BUDGET_SECONDS = float(os.getenv("CANVAS_EXTRACT_RUN_BUDGET_SECONDS", "300"))
_PER_PAGE_TIMEOUT = float(os.getenv("CANVAS_EXTRACT_PAGE_TIMEOUT_SECONDS", "30"))
_PER_CALL_TIMEOUT = float(os.getenv("CANVAS_EXTRACT_CALL_TIMEOUT_SECONDS", "12"))
# The RPC cluster router (Surface llama-server over the RPC fabric) needs its
# OWN, much larger budget than the fast local Ollama path — it runs a bigger
# model and is ~1 tok/s at term start. Capping it at the 12s Ollama timeout (the
# old bug) guaranteed every RPC attempt timed out, so RPC never actually served
# a page. Default 0 = RPC fallback DISABLED for the bulk per-page crawl (it would
# blow the per-page budget); set >0 to enable RPC as a real fallback, and use a
# single-page/offline reprocess when you want the cluster to do the work.
_RPC_CALL_TIMEOUT = float(os.getenv("CANVAS_EXTRACT_RPC_TIMEOUT_SECONDS", "0"))
# The first local call must load the model into memory (~6s for LFM2.5-1.2B on
# this box). Give the cold call a bigger budget so it isn't guaranteed to time
# out and silently demote every page to the regex fallback.
_COLD_CALL_TIMEOUT = float(os.getenv("CANVAS_EXTRACT_COLD_TIMEOUT_SECONDS", "40"))
# Keep the model resident between pages so calls 2..N stay warm (~3s each).
_OLLAMA_KEEP_ALIVE = os.getenv("CANVAS_EXTRACT_OLLAMA_KEEP_ALIVE", "30m")
_OLLAMA_NUM_PREDICT = int(os.getenv("CANVAS_EXTRACT_OLLAMA_NUM_PREDICT", "512"))

# Direct local LLM endpoint (Ollama-native /api/generate).
_OLLAMA_GENERATE_URL = os.getenv(
    "CANVAS_EXTRACT_OLLAMA_URL", "http://127.0.0.1:11434/api/generate"
)
# NOTE: this must be the exact Ollama tag (`ollama list`), not a friendly alias.
# A wrong name returns HTTP 404 and silently demotes every page to the regex
# fallback. Default is LFM2.5-1.2B: measured ~3s/page warm on this box, which is
# what lets an all-courses crawl finish in budget. The 2.6B is available by
# setting CANVAS_EXTRACT_OLLAMA_MODEL=hf.co/LiquidAI/LFM2.5-2.6B-GGUF:Q4_K_M, but
# it benchmarks at ~3 tok/s + reasoning-mode here (~100s/page) and will starve a
# multi-course pass — prefer it only for single-page/offline re-processing.
_OLLAMA_MODEL = os.getenv(
    "CANVAS_EXTRACT_OLLAMA_MODEL",
    "hf.co/LiquidAI/LFM2.5-1.2B-Instruct-GGUF:latest",
)

# One-shot warm-up latch: the first _ollama_generate of a run gets the cold
# budget; subsequent calls use the normal (shorter) per-call timeout.
_model_warmed = threading.Event()

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

        # 2. Named month with day: "Unit 1 Quiz 1 - Friday, Aug. 14th", "Unit 1 Derivita Assignment - Tuesday, Sept. 1st"
        named_date_match = re.search(
            r"([A-Za-z0-9\s/_-]+?)\s*[-:—]\s*(?:(?:Mon|Tue|Wed|Thu|Fri|Monday|Tuesday|Wednesday|Thursday|Friday)\s*,?\s*)?(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
            line,
            re.IGNORECASE,
        )
        if named_date_match:
            item_name = named_date_match.group(1).strip()
            mon_str = named_date_match.group(2).lower()
            months = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
            m = months.get(mon_str[:3], 8)
            d = int(named_date_match.group(3))
            if 1 <= m <= 12 and 1 <= d <= 31 and len(item_name) >= 3:
                results.append(
                    {
                        "title": item_name,
                        "due_date": f"{year}-{m:02d}-{d:02d}",
                        "task_type": _normalize_task_type(item_name),
                    }
                )
                continue

        # 3. Section date heading. Two accepted forms, deliberately strict about
        #    the dot separator so lesson/section numbers ("lesson 1.2", "p. 32",
        #    "pgs. 6-7") are NOT mistaken for dates:
        #      a) slash/dash date, optional weekday: "Monday, 8/17", "8-17"
        #      b) dotted date ONLY with a weekday anchor: "Monday 8.17"
        #    A bare "1.2" with no weekday is rejected.
        date_match = re.search(
            r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday)?\s*,?\s*(\d{1,2})[/-](\d{1,2})\b",
            line,
            re.IGNORECASE,
        )
        if not date_match:
            # Dotted form requires an explicit weekday immediately before it.
            date_match = re.search(
                r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday)\s*,?\s*(\d{1,2})\.(\d{1,2})\b",
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

    Sends ``keep_alive`` so the model stays resident between pages (the first
    call pays a one-time cold-load cost; the rest are warm). The first call of a
    run also gets an enlarged timeout so cold-load can't guarantee a fallback.
    """
    import requests  # lazy: keeps module import cheap and test-friendly

    # First call of the run gets the cold-load budget; later calls the normal one.
    if not _model_warmed.is_set():
        timeout = max(timeout, _COLD_CALL_TIMEOUT)

    payload = {
        "model": _OLLAMA_MODEL,
        "prompt": prompt,
        "system": system_prompt,
        "stream": False,
        "keep_alive": _OLLAMA_KEEP_ALIVE,
        "options": {"temperature": 0.0, "num_predict": _OLLAMA_NUM_PREDICT},
    }
    try:
        resp = requests.post(_OLLAMA_GENERATE_URL, json=payload, timeout=timeout)
        if resp.status_code != 200:
            # A wrong model tag returns 404 here; surface it loudly once so a
            # misconfiguration doesn't masquerade as "LLM unavailable".
            logger.warning(
                "Ollama /api/generate returned HTTP %s for model %r",
                resp.status_code, _OLLAMA_MODEL,
            )
            return ""
        data = resp.json()
        text = data.get("response", "") if isinstance(data, dict) else ""
        _model_warmed.set()  # model is loaded; subsequent calls are warm
        return text.strip() if isinstance(text, str) else ""
    except Exception as exc:  # network, decode, timeout
        logger.debug("Ollama /api/generate failed: %s", type(exc).__name__)
        return ""


def warm_up_model(timeout: float | None = None) -> bool:
    """Pre-load the local model once before a crawl so per-page calls are warm.

    Returns True if the model responded (now resident). Call this at the start of
    a calendar pass so the cold load is paid once, not risked on the first real
    page. Safe to call repeatedly; a no-op once warmed.
    """
    if _model_warmed.is_set():
        return True
    out = _ollama_generate("ping", "Reply with OK.", timeout or _COLD_CALL_TIMEOUT)
    if out:
        _model_warmed.set()  # latch even if the inner call's own set() was mocked out
    return _model_warmed.is_set()


def _call_local_llm(prompt: str, system_prompt: str, timeout: float) -> str:
    """Primary local inference: direct Ollama endpoint, then optional RPC router.

    Both are local (private-data safe). The fast Ollama path uses the passed
    ``timeout``; the RPC cluster router uses its OWN budget (_RPC_CALL_TIMEOUT)
    because it runs a larger model at ~1 tok/s and would otherwise be killed by
    the short per-page Ollama timeout. RPC is skipped entirely when its budget is
    0 (the default for the bulk crawl). Returns "" when neither path yields text,
    routing the caller to the heuristic fallback.
    """
    text = _ollama_generate(prompt, system_prompt, timeout)
    if text:
        return text
    if _RPC_CALL_TIMEOUT <= 0:
        return ""  # RPC fallback disabled for this (bulk) path
    try:
        from llm_router import call_local_rpc

        return call_local_rpc(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=512,
            temperature=0.0,
            timeout=_RPC_CALL_TIMEOUT,
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
        item["official"] = True
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


# ═════════════════════════════════════════════════════════════════════════════
# Hybrid DOM + Vision extraction
# -----------------------------------------------------------------------------
# Some Canvas pages carry their schedule only in non-textual embeds: a daily
# agenda image, a flattened syllabus graphic, a Google Slides canvas viewer, or
# an image-only PDF. DOM text extraction returns little or nothing for those. The
# hybrid path detects such embeds and, when the DOM yields too little text, routes
# a rendered screenshot to the local multimodal model (LFM2.5-VL) for OCR-grounded
# extraction, then feeds the transcription through the same LLM/heuristic parser.
#
# The screenshot is supplied by the caller (the BrowserNavigator page), so this
# module stays free of any browser dependency and fully mockable in tests.
# ═════════════════════════════════════════════════════════════════════════════

import base64  # noqa: E402  (kept local to the hybrid section)

_VISION_URL = os.getenv("CANVAS_VISION_URL", "http://127.0.0.1:11434/api/generate")
_VISION_MODEL = os.getenv("CANVAS_VISION_MODEL", "LFM2.5-VL")
_VISION_TIMEOUT = float(os.getenv("CANVAS_VISION_TIMEOUT_SECONDS", "45"))

# A page whose compacted DOM text is shorter than this is treated as
# "effectively textless" and becomes a candidate for the vision fallback.
_MIN_DOM_TEXT_CHARS = int(os.getenv("CANVAS_MIN_DOM_TEXT_CHARS", "40"))

# Embed sources that typically render assignment data as pixels, not DOM text.
_VISUAL_EMBED_HOST_MARKERS = (
    "docs.google.com/presentation", "drive.google.com", "canva.com",
    "slides.com", "prezi.com", "/preview", "/pubembed", "docs.google.com/viewer",
)


def _detect_visual_embeds(html_body: str) -> list[dict[str, str]]:
    """Find non-textual embeds whose content may need vision OCR.

    Returns a list of ``{"kind", "src", "title"}`` for iframes/canvases/images
    that plausibly hold a rendered agenda/syllabus/slide the DOM parser cannot
    read. Pure parsing — no network, no browser.
    """
    if not html_body or not isinstance(html_body, str):
        return []
    soup = BeautifulSoup(html_body, "html.parser")
    embeds: list[dict[str, str]] = []
    seen: set[str] = set()

    for ifr in soup.find_all("iframe"):
        src = str(ifr.get("src", "") or "")
        if not src or src in seen:
            continue
        seen.add(src)
        low = src.lower()
        if any(marker in low for marker in _VISUAL_EMBED_HOST_MARKERS):
            embeds.append(
                {"kind": "iframe", "src": src, "title": str(ifr.get("title", "") or "")}
            )

    # A <canvas> element or a lone large image with no surrounding text is a
    # classic flattened-agenda signal.
    for canvas_el in soup.find_all("canvas"):
        embeds.append({"kind": "canvas", "src": "", "title": str(canvas_el.get("aria-label", "") or "")})

    for img in soup.find_all("img"):
        src = str(img.get("src", "") or "")
        if src.startswith("data:") or src in seen:
            continue
        alt = str(img.get("alt", "") or "")
        # Heuristic: agenda/syllabus/schedule graphics are the interesting ones.
        if any(k in f"{src} {alt}".lower() for k in ("agenda", "syllabus", "schedule", "calendar", "slide")):
            seen.add(src)
            embeds.append({"kind": "image", "src": src, "title": alt})

    return embeds


def _call_vision_llm(image_bytes: bytes, prompt: str, timeout: float) -> str:
    """Send a rendered page/element screenshot to the local multimodal endpoint.

    Network seam — monkeypatched in tests. Returns an empty string on any failure
    so the caller degrades gracefully. Uses temperature 0.0 for determinism.
    """
    if not image_bytes:
        return ""
    import requests

    payload = {
        "model": _VISION_MODEL,
        "prompt": prompt,
        "images": [base64.b64encode(image_bytes).decode("ascii")],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    try:
        resp = requests.post(_VISION_URL, json=payload, timeout=timeout)
        if resp.status_code != 200:
            logger.debug("Canvas vision endpoint returned HTTP %s", resp.status_code)
            return ""
        data = resp.json()
        text = data.get("response", "") if isinstance(data, dict) else ""
        return text.strip() if isinstance(text, str) else ""
    except Exception as exc:
        logger.debug("Canvas vision endpoint failed: %s", type(exc).__name__)
        return ""


_VISION_PROMPT = (
    "This is a screenshot of a Canvas course page, a daily-agenda slide, or a "
    "syllabus graphic. Read ALL visible text, including tables and slides, and "
    "extract any tests, quizzes, readings, homework, or projects with their due "
    "dates. Assume the current year is {year}. Respond ONLY with a valid JSON "
    'array of objects with keys "title", "due_date" (YYYY-MM-DD), and '
    '"task_type" (one of Test, Project, Reading, Assignment). If none, output [].'
)


def _rows_from_raw(raw: str) -> list[dict]:
    """Parse + normalize an LLM/vision JSON response into clean task rows."""
    parsed = _extract_json_array(raw)
    if not parsed:
        return []
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
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
        rows.append(
            {"title": title, "due_date": iso, "task_type": _normalize_task_type(row.get("task_type"))}
        )
    return rows


def extract_assignments_hybrid(
    course_id: str,
    course_name: str,
    page_title: str,
    page_url: str,
    html_body: str,
    *,
    screenshot_provider: "Callable[[], bytes] | None" = None,
) -> list[dict]:
    """DOM-first extraction with a vision fallback for image-only pages.

    1. Run the normal DOM/LLM/heuristic pipeline
       (:func:`extract_assignments_from_html`).
    2. If that returns rows, use them (DOM is cheaper and more precise).
    3. Otherwise, if the page has visual embeds *or* effectively no DOM text and a
       ``screenshot_provider`` is supplied, capture a screenshot, OCR it via the
       local vision model, and parse the transcription.

    ``screenshot_provider`` is a zero-arg callable returning PNG bytes (typically
    ``lambda: navigator.page.screenshot()``). When omitted, the vision path is
    skipped and DOM results (possibly empty) are returned.
    """
    dom_rows = extract_assignments_from_html(
        course_id, course_name, page_title, page_url, html_body
    )
    if dom_rows:
        return dom_rows

    structured_text = _parse_html_with_structure_and_links(html_body)
    embeds = _detect_visual_embeds(html_body)
    needs_vision = bool(embeds) or len(structured_text) < _MIN_DOM_TEXT_CHARS

    if not (needs_vision and screenshot_provider is not None):
        return dom_rows  # nothing more we can do; return DOM result ([] here)

    if _budget_remaining() <= 3.0:
        return dom_rows

    try:
        image_bytes = screenshot_provider()
    except Exception as exc:
        logger.debug("Screenshot provider failed for %s: %s", page_url, type(exc).__name__)
        return dom_rows

    raw = _call_vision_llm(
        image_bytes,
        _VISION_PROMPT.format(year=_ASSUMED_YEAR),
        _VISION_TIMEOUT,
    )
    vision_rows = _rows_from_raw(raw)
    return _decorate(vision_rows, course_id, course_name, page_url)
