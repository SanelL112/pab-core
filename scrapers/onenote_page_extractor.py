"""OneNote 2D spatial layout parser and multi-modal task extractor.

OneNote pages are not linear documents. Graph returns HTML where free-floating
text is wrapped in absolutely positioned ``<div>`` blocks::

    <div style="position:absolute;left:120px;top:340px">U1 Test 9/4</div>

Reading that markup top-to-bottom in source order scrambles the human reading
order, so this module:

1. :func:`parse_spatial_layout` — extracts every positioned block, reads its
   ``left``/``top`` pixel coordinates, and sorts the blocks top-to-bottom then
   left-to-right (with a configurable row band so items on the same visual line
   stay in reading order) to reconstruct natural text flow.
2. :func:`detect_visual_content` — flags digital-ink canvases, handwritten
   stylus notes, and flattened PDF/image printouts.
3. :func:`extract_tasks_from_page` — orchestrates routing: if the page yields
   extractable positioned/plain text it goes to the local *text* LLM (reusing
   the Canvas extraction/normalization pipeline); if the page is image-/ink-only
   it is routed to the local *multimodal* endpoint (``LFM2.5-VL``) with a
   rendered snapshot.

The text LLM/heuristic/date-normalization logic is shared with
``canvas_page_extractor`` so both sources emit identically shaped task dicts.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import sys
from typing import Any, Callable

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# Reuse the Canvas pipeline's LLM seam, JSON recovery, and normalization so both
# sources stay consistent and there is a single place to fix extraction bugs.
from scrapers.canvas_page_extractor import (  # noqa: E402
    _budget_remaining,
    _call_local_llm,
    _extract_json_array,
    _normalize_date,
    _normalize_task_type,
    _PER_CALL_TIMEOUT,
)

# Cluster multimodal endpoint: Surface llama-server (LFM2-VL) via Pi ip_forward.
_VISION_URL = os.getenv("ONENOTE_VISION_URL", "http://10.42.0.1:8081")
_VISION_MODEL = os.getenv("ONENOTE_VISION_MODEL", "LFM2-VL-1.6B")
_VISION_TIMEOUT = float(os.getenv("ONENOTE_VISION_TIMEOUT_SECONDS", "300"))

# Row band (px): text blocks whose ``top`` differs by less than this are treated
# as the same visual line and ordered by ``left``.
_ROW_BAND_PX = int(os.getenv("ONENOTE_ROW_BAND_PX", "12"))

# Minimum characters of recovered text before we trust the DOM over vision.
_MIN_TEXT_CHARS = int(os.getenv("ONENOTE_MIN_TEXT_CHARS", "8"))


# ── Coordinate parsing ────────────────────────────────────────────────────────
_POS_RE = re.compile(r"position\s*:\s*absolute", re.IGNORECASE)
_LEFT_RE = re.compile(r"left\s*:\s*(-?\d+(?:\.\d+)?)\s*px", re.IGNORECASE)
_TOP_RE = re.compile(r"top\s*:\s*(-?\d+(?:\.\d+)?)\s*px", re.IGNORECASE)


def _coord(style: str, pattern: re.Pattern) -> float | None:
    if not style:
        return None
    m = pattern.search(style)
    return float(m.group(1)) if m else None


class _Block:
    """A positioned text block with its reconstructed coordinates."""

    __slots__ = ("top", "left", "text")

    def __init__(self, top: float, left: float, text: str) -> None:
        self.top = top
        self.left = left
        self.text = text


def parse_spatial_layout(html_body: str) -> str:
    """Reconstruct natural reading order from absolutely positioned blocks.

    Blocks are sorted top-to-bottom, then left-to-right within a row band, so
    scrambled source order (or deliberately shuffled CSS coordinates) still reads
    correctly. Blocks without positioning fall back to document order and are
    appended after the positioned content.
    """
    if not html_body or not isinstance(html_body, str):
        return ""

    soup = BeautifulSoup(html_body, "html.parser")

    positioned: list[_Block] = []
    unpositioned: list[str] = []

    # Only consider leaf-ish text containers to avoid counting a parent and its
    # child twice. OneNote emits text in <div>/<p>/<span> with inline styles.
    for node in soup.find_all(["div", "p", "span"]):
        style = str(node.get("style") or "")
        # Skip a positioned ancestor whose text is fully owned by positioned
        # descendants (prevents double-counting the same words).
        has_positioned_child = any(
            _POS_RE.search(str(child.get("style") or ""))
            for child in node.find_all(["div", "p", "span"])
        )
        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if _POS_RE.search(style):
            if has_positioned_child:
                continue
            top = _coord(style, _TOP_RE)
            left = _coord(style, _LEFT_RE)
            positioned.append(_Block(top if top is not None else 0.0,
                                     left if left is not None else 0.0, text))
        elif not has_positioned_child and node.name in {"p", "div"}:
            # Plain (non-OneNote-canvas) content, e.g. a pasted table or list.
            unpositioned.append(text)

    ordered = _sort_blocks(positioned)
    lines = [b.text for b in ordered]

    # De-duplicate unpositioned text that already appeared in positioned blocks.
    positioned_join = " ".join(lines)
    for extra in unpositioned:
        if extra and extra not in positioned_join:
            lines.append(extra)

    return "\n".join(lines).strip()


def _sort_blocks(blocks: list[_Block]) -> list[_Block]:
    """Top-to-bottom, then left-to-right within a row band."""
    if not blocks:
        return []
    # Primary sort by top; group into row bands; sort each band by left.
    by_top = sorted(blocks, key=lambda b: (b.top, b.left))
    rows: list[list[_Block]] = []
    for block in by_top:
        if rows and abs(block.top - rows[-1][0].top) <= _ROW_BAND_PX:
            rows[-1].append(block)
        else:
            rows.append([block])
    ordered: list[_Block] = []
    for row in rows:
        ordered.extend(sorted(row, key=lambda b: b.left))
    return ordered


# ── Visual / ink detection ─────────────────────────────────────────────────────
_INK_MARKERS = (
    "inkml", "data-ink", "application/inkml", "<ink", "data-render-original-src",
    "data:application/x-msink", "onenote-ink",
)


def detect_visual_content(html_body: str) -> dict[str, bool]:
    """Classify a OneNote page's visual composition.

    Returns flags:
      * ``has_ink``       — digital-ink / handwritten stylus strokes present.
      * ``has_images``    — one or more embedded images (incl. flattened PDF
                            printouts, which OneNote stores as page-sized images).
      * ``has_text``      — extractable positioned/plain DOM text present.
      * ``image_only``    — images/ink present but effectively no DOM text, so the
                            page must be routed to the vision model.
    """
    if not html_body or not isinstance(html_body, str):
        return {"has_ink": False, "has_images": False, "has_text": False, "image_only": False}

    low = html_body.lower()
    has_ink = any(marker in low for marker in _INK_MARKERS)

    soup = BeautifulSoup(html_body, "html.parser")
    images = soup.find_all("img")
    objects = soup.find_all("object")
    has_images = bool(images) or bool(objects)

    text = parse_spatial_layout(html_body)
    has_text = len(text) >= _MIN_TEXT_CHARS

    image_only = (has_images or has_ink) and not has_text
    return {
        "has_ink": has_ink,
        "has_images": has_images,
        "has_text": has_text,
        "image_only": image_only,
    }


# ── Vision routing seam ─────────────────────────────────────────────────────────
def _call_vision_llm(image_bytes: bytes, prompt: str, timeout: float) -> str:
    """Send a rendered page snapshot to the cluster's vision endpoint.

    The Surface Pro runs llama-server with LFM2-VL-1.6B (+mmproj) on port
    8081 (see the llama-vision user service); the Dell reaches it through the
    Pi's ip_forward. OpenAI-compatible chat API with a base64 data URL.

    Network seam — unit tests monkeypatch this. Returns an empty string on any
    failure so the caller degrades gracefully to no tasks.
    """
    if not image_bytes:
        return ""
    import requests

    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 512,
        "temperature": 0.0,
        "stream": False,
    }
    try:
        resp = requests.post(
            _VISION_URL.rstrip("/") + "/v1/chat/completions",
            json=payload,
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.debug("Vision endpoint returned HTTP %s", resp.status_code)
            return ""
        data = resp.json()
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            return ""
        message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
        text = message.get("content", "") if isinstance(message, dict) else ""
        return text.strip() if isinstance(text, str) else ""
    except Exception as exc:
        logger.debug("Vision endpoint failed: %s", type(exc).__name__)
        return ""


_VISION_PROMPT = (
    "This is a snapshot of a student's handwritten or image-only OneNote page. "
    "Read all visible text, including handwriting, and extract any tests, quizzes, "
    "readings, homework, or projects with their due dates. "
    "Respond ONLY with a valid JSON array of objects with keys "
    '"title", "due_date" (YYYY-MM-DD), and "task_type" '
    "(one of Test, Project, Reading, Assignment). If none, output []."
)

_TEXT_SYSTEM_PROMPT = (
    "You extract calendar tasks and output ONLY a valid JSON array. No prose."
)


def _text_prompt(page_title: str, text: str) -> str:
    from scrapers.canvas_page_extractor import _ASSUMED_YEAR

    return f"""You are helping a student organize their calendar.
Below is text read from a OneNote page titled '{page_title}', already sorted into natural reading order.
Extract any upcoming tests, quizzes, readings, homework, or projects with their due dates.
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
{text}
"""


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
            {
                "title": title,
                "due_date": iso,
                "task_type": _normalize_task_type(row.get("task_type")),
            }
        )
    return rows


def _decorate(items: list[dict], page_id: str, page_title: str, web_url: str) -> list[dict]:
    import hashlib

    out: list[dict] = []
    for item in items:
        if not item.get("title") or not item.get("due_date"):
            continue
        item = dict(item)
        item["source"] = "onenote"
        item["course"] = page_title
        id_str = f"{page_id}-{item['title']}-{item['due_date']}"
        item["id"] = f"onenote-{hashlib.md5(id_str.encode()).hexdigest()[:12]}"
        item["url"] = web_url or ""
        item["official"] = False
        out.append(item)
    return out


def extract_tasks_from_page(
    page: dict[str, Any],
    html_body: str,
    *,
    render_snapshot: Callable[[dict[str, Any], str], bytes] | None = None,
) -> list[dict]:
    """Extract tasks from a single OneNote page, routing by content type.

    Args:
        page: page metadata dict from ``OneNoteClient.list_pages`` (``id``,
            ``title``, ``links``).
        html_body: raw page HTML from ``OneNoteClient.get_page_html``.
        render_snapshot: optional callable ``(page, html) -> PNG bytes`` used for
            image-/ink-only pages. When omitted, such pages yield no tasks (the
            caller can supply a renderer that rasterizes the page).

    Returns decorated, normalized task dicts (never raises for a bad page).
    """
    page_id = str(page.get("id") or "")
    page_title = str(page.get("title") or "Untitled")
    web_url = ""
    links = page.get("links")
    if isinstance(links, dict):
        web = links.get("oneNoteWebUrl")
        if isinstance(web, dict):
            web_url = str(web.get("href") or "")

    profile = detect_visual_content(html_body)

    # 1. Text route — positioned/plain DOM text is present.
    visual_page = bool(profile["has_ink"] or profile["has_images"])
    if profile["has_text"]:
        text = parse_spatial_layout(html_body)
        remaining = _budget_remaining()
        if remaining > 3.0:
            raw = _call_local_llm(
                _text_prompt(page_title, text),
                _TEXT_SYSTEM_PROMPT,
                min(_PER_CALL_TIMEOUT, remaining),
            )
            rows = _rows_from_raw(raw)
            if rows:
                return _decorate(rows, page_id, page_title, web_url)
        # Fall back to the shared deterministic heuristic on the sorted text.
        from scrapers.canvas_page_extractor import _heuristic_rule_extraction

        heur = _heuristic_rule_extraction(text)
        clean: list[dict] = []
        for row in heur:
            iso = _normalize_date(row.get("due_date"))
            title = str(row.get("title") or "").strip()
            if iso and title:
                clean.append(
                    {"title": title, "due_date": iso, "task_type": _normalize_task_type(row.get("task_type"))}
                )
        if clean or not visual_page or render_snapshot is None:
            return _decorate(clean, page_id, page_title, web_url)
        # Text carried only a header; the real content is ink/images — let
        # the vision route read the rendered page below.

    # 2. Vision route — ink/image content (image-only pages, and text pages
    # whose extraction came up empty while the page carries visuals).
    if visual_page and render_snapshot is not None:
        try:
            snapshot = render_snapshot(page, html_body)
        except Exception as exc:
            logger.debug("Snapshot render failed for %s: %s", page_id, exc)
            snapshot = b""
        raw = _call_vision_llm(snapshot, _VISION_PROMPT, _VISION_TIMEOUT)
        rows = _rows_from_raw(raw)
        return _decorate(rows, page_id, page_title, web_url)

    # 3. Nothing extractable.
    return []

