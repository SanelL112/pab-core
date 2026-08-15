import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, date
import urllib.request
import urllib.error
import html
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# ── Result cache ─────────────────────────────────────────────────────────────
try:
    from config import CACHE_DIR
    _CACHE_PATH = CACHE_DIR / "canvas_page_extractions.json"
except Exception:
    _CACHE_PATH = None

_cache_lock = threading.Lock()

_RUN_BUDGET_SECONDS = float(os.getenv("CANVAS_EXTRACT_RUN_BUDGET_SECONDS", "180"))
_PER_PAGE_TIMEOUT = float(os.getenv("CANVAS_EXTRACT_PAGE_TIMEOUT_SECONDS", "30"))
_run_state = threading.local()


def reset_extraction_budget() -> None:
    """Start a fresh per-run extraction budget. Call once per calendar pass."""
    _run_state.deadline = time.monotonic() + _RUN_BUDGET_SECONDS


def _budget_remaining() -> float:
    deadline = getattr(_run_state, "deadline", None)
    if deadline is None:
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
        if len(cache) > 300:
            cache = dict(list(cache.items())[-300:])
        _CACHE_PATH.write_text(json.dumps(cache))
    except OSError as exc:
        logger.debug("Could not persist Canvas extraction cache: %s", exc)


def _extract_json_array(raw: str) -> list | None:
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


def _fetch_external_link_text(url: str, timeout: float = 6.0) -> str:
    """Attempt to fetch plaintext from public Google Docs, Slides, or published embeds."""
    if not url or not isinstance(url, str):
        return ""
    
    # 1. Published Google Slides (/presentation/d/e/2PACX-.../pubembed)
    if "docs.google.com/presentation/d/e/" in url or "pubembed" in url:
        pub_url = url.split("?")[0].replace("pubembed", "pub")
        try:
            req = urllib.request.Request(
                pub_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_html = resp.read().decode("utf-8", errors="ignore")
                decoded = raw_html.encode("utf-8").decode("unicode_escape", errors="ignore")
                decoded = html.unescape(decoded)
                matches = re.findall(r'\"Plans[^\"]+\"', decoded)
                if matches:
                    return "\n".join(matches).replace("\\n", "\n")[:4000]
                tokens = re.findall(r'[\w\s.,;:!?\(\)/\'\"#\-]{4,}', decoded)
                meaningful = [t.strip() for t in tokens if any(k in t.lower() for k in ['quiz', 'test', 'exam', 'homework', 'u1q', 'due', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'plans', 'upcoming'])]
                if meaningful:
                    return "\n".join(meaningful[:35])
        except Exception as e:
            logger.debug("Failed to fetch published Google Slide %s: %s", pub_url, e)
            return ""

    # 2. Standard Google Docs & Google Slides export
    export_url = None
    doc_match = re.search(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)", url)
    if doc_match:
        doc_id = doc_match.group(1)
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    else:
        slide_match = re.search(r"docs\.google\.com/presentation/d/([a-zA-Z0-9_-]+)", url)
        if slide_match:
            slide_id = slide_match.group(1)
            export_url = f"https://docs.google.com/presentation/d/{slide_id}/export/txt"

    if not export_url:
        return ""

    try:
        req = urllib.request.Request(
            export_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="ignore")
            return content.strip()[:2500]
    except Exception as e:
        logger.debug("Failed to fetch external doc %s: %s", export_url, e)
        return ""


def _parse_html_with_structure_and_links(html_body: str) -> str:
    """Parse HTML preserving table structure and resolving embedded links/iframes."""
    if not html_body or not isinstance(html_body, str):
        return ""
    
    soup = BeautifulSoup(html_body, "html.parser")
    
    # Tables -> format as text rows
    for table in soup.find_all("table"):
        rows_text = []
        for tr in table.find_all("tr"):
            cols = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if any(cols):
                rows_text.append(" | ".join(cols))
        if rows_text:
            table_summary = "\n[TABLE DATA]:\n" + "\n".join(rows_text) + "\n"
            table.replace_with(soup.new_string(table_summary))

    # Iframes and External Links
    embedded_snippets = []
    seen_urls = set()

    for ifr in soup.find_all("iframe"):
        src = ifr.get("src", "")
        title = ifr.get("title", "Embedded Frame")
        if src and src not in seen_urls:
            seen_urls.add(src)
            doc_text = _fetch_external_link_text(src)
            if doc_text:
                embedded_snippets.append(f"\n--- Embedded Doc ({title}): ---\n{doc_text}\n--- End Embedded Doc ---")
            else:
                embedded_snippets.append(f"[Embedded Frame: {title} -> {src}]")

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if href and href not in seen_urls:
            seen_urls.add(href)
            if any(k in href.lower() for k in ["docs.google.com/document", "docs.google.com/presentation"]):
                doc_text = _fetch_external_link_text(href)
                if doc_text:
                    embedded_snippets.append(f"\n--- Linked Doc ({text}): ---\n{doc_text}\n--- End Linked Doc ---")
            elif any(k in href.lower() for k in ["forms.gle", "docs.google.com/forms", "onenote", "canva.com", "gateway.cengage.com"]):
                embedded_snippets.append(f"[Linked Resource: '{text}' -> {href}]")

    base_text = soup.get_text(separator="\n", strip=True)
    full_text = base_text + ("\n\n" + "\n".join(embedded_snippets) if embedded_snippets else "")
    return re.sub(r"\n{3,}", "\n\n", full_text).strip()


def _heuristic_rule_extraction(text: str) -> list[dict]:
    """Fallback rule-based extractor if LLM RPC is offline or busy."""
    results = []
    year = datetime.now().year
    
    # Check for patterns like "U1Q1 (8.17)", "WA1a (8.17)", "U1Q2 (8.26/8.27)", "Monday, 8/17 - Quiz"
    lines = text.split("\n")
    current_date = None
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 1. Inline item with date parenthesized: e.g. "U1Q1 (8.17)" or "WA1a (8.17)" or "U1 Test (9.4)"
        inline_match = re.search(r"([A-Za-z0-9\s/_-]+?)\s*\((?:(?:Mon|Tue|Wed|Thu|Fri)?\s*)?(\d{1,2})[./-](\d{1,2})\)", line)
        if inline_match:
            item_name = inline_match.group(1).strip()
            m, d = int(inline_match.group(2)), int(inline_match.group(3))
            if 1 <= m <= 12 and 1 <= d <= 31 and len(item_name) >= 3:
                ttype = "Test" if any(k in item_name.lower() for k in ["q", "quiz", "test", "exam"]) else "Assignment"
                results.append({
                    "title": item_name,
                    "due_date": f"{year}-{m:02d}-{d:02d}",
                    "task_type": ttype
                })
                continue

        # 2. Section date heading: "Monday, 8/17 -" or "8/17"
        date_match = re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday)?\s*,?\s*(\d{1,2})[/.-](\d{1,2})\b", line, re.IGNORECASE)
        if date_match:
            month, day = int(date_match.group(1)), int(date_match.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                try:
                    current_date = f"{year}-{month:02d}-{day:02d}"
                except ValueError:
                    pass
        
        # 3. Actionable keyword in line
        if any(k in line.lower() for k in ["quiz", "test", "exam", "homework", "due", "submit", "assignment", "formative", "summative", "read", "lor activity", "u1q", "wa1"]):
            cleaned = re.sub(r"^[0-9]+[.)]\s*", "", line)
            cleaned = re.sub(r"^(Homework|Quiz|Test)\s*[-:]\s*", "", cleaned, flags=re.IGNORECASE).strip()
            
            task_type = "Assignment"
            if any(k in line.lower() for k in ["quiz", "test", "exam", "formative", "summative", "u1q"]):
                task_type = "Test"
            elif any(k in line.lower() for k in ["read", "reading"]):
                task_type = "Reading"
            elif any(k in line.lower() for k in ["project", "presentation"]):
                task_type = "Project"

            if current_date and len(cleaned) > 3:
                results.append({
                    "title": cleaned[:100],
                    "due_date": current_date,
                    "task_type": task_type
                })
    return results


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


def extract_assignments_from_html(
    course_id: str,
    course_name: str,
    page_title: str,
    page_url: str,
    html_body: str,
) -> list[dict]:
    """Extract assignments from Canvas page HTML & embedded documents using local RPC + rule fallback."""
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

    clean_rows = []
    remaining = _budget_remaining()
    
    if remaining > 3.0:
        prompt = f"""You are helping a high school student organize their calendar.
Below is text extracted from a Canvas course page or announcement titled '{page_title}' for the course '{course_name}'.
Extract any upcoming tests, quizzes, readings, homework, or assignments along with their due dates.
Assume current year is {datetime.now().year}.
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
{structured_text[:3500]}
"""
        try:
            from llm_router import call_local_rpc
            raw = call_local_rpc(
                prompt=prompt,
                system_prompt="You extract calendar tasks and output ONLY a valid JSON array. No prose.",
                max_tokens=512,
                temperature=0.0,
                timeout=min(12.0, remaining),
            )
            parsed = _extract_json_array(raw)
            if parsed is not None:
                clean_rows = [
                    {"title": r["title"], "due_date": r["due_date"], "task_type": r.get("task_type", "Assignment")}
                    for r in parsed
                    if isinstance(r, dict) and r.get("title") and r.get("due_date")
                ]
        except Exception as exc:
            logger.debug("Canvas RPC extraction failed, using fallback: %s", exc)

    # If RPC returned nothing or failed, use heuristic rule extraction
    if not clean_rows:
        clean_rows = _heuristic_rule_extraction(structured_text)

    with _cache_lock:
        cache = _load_cache()
        cache[cache_key] = clean_rows
        _save_cache(cache)

    return _decorate(clean_rows, course_id, course_name, page_url)
