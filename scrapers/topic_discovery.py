"""Per-class study-topic discovery.

Grounds topic proposals in each class's OWN material — the Canvas page
extraction cache, the OneNote harvest cache, and calendar course labels —
instead of one weak local-model pass over mixed summaries (the old approach
that kept proposing irrelevant topics).

Deterministic first: recent page/task titles from a class are already
candidate topics.  The online provider chain (free models) is used only to
name them cleanly; if it fails the deterministic candidates are returned
as-is, so discovery never comes back empty when material exists.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import dateutil.parser as _date_parser

logger = logging.getLogger(__name__)

_TOPIC_NAME_RE = re.compile(r"[^A-Za-z0-9 +&:'\-]")
_MIN_MATERIAL_CHARS = 12


def _load_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _clean_title(title: str) -> str:
    title = _TOPIC_NAME_RE.sub(" ", str(title or ""))
    return re.sub(r"\s+", " ", title).strip(" -,:;")


_HASH_KEY_RE = re.compile(r"^[0-9a-f]{16,}$")


def _is_past_date_title(title: str) -> bool:
    """True if *title* is nothing but a date that has already passed.

    Canvas/OneNote extractions include calendar/agenda pages whose entire
    title is a date (e.g. ``Aug 24``, ``2026-08-24``). On Aug 31 those read
    as stale study opportunities, so drop them.  Titles that merely *contain*
    a date among real words (``Unit 1 Week 4 Aug 24 - 28``) are NOT dropped —
    strict parsing rejects them, which is the safe default.
    """
    cleaned = re.sub(r"\s+", " ", title or "").strip()
    if not cleaned:
        return False
    try:
        parsed = _date_parser.parse(cleaned, fuzzy=False)
    except (ValueError, TypeError, OverflowError):
        return False
    return parsed.date() < datetime.now().date()

# class material key -> human label inferred by the refinement model
_LAST_LABELS: dict[str, str] = {}


def _load_course_names(cache_dir: Path) -> dict[str, str]:
    """course-id -> name, refreshed best-effort from the daemon's Canvas session."""
    cached = _load_json(cache_dir / "canvas_course_names.json")
    if isinstance(cached, dict) and cached:
        return {str(k): str(v) for k, v in cached.items()}
    try:
        import requests

        resp = requests.get(
            "http://127.0.0.1:8976/request",
            params={"path": "/api/v1/courses?per_page=50"},
            timeout=8,
        )
        data = resp.json()
        if isinstance(data, list):
            names = {str(c.get("id")): str(c.get("name") or "") for c in data if c.get("id")}
            if names:
                (cache_dir / "canvas_course_names.json").write_text(json.dumps(names))
                return names
    except Exception:
        pass
    return {}


def _material_from_caches(cache_dir: Path, cutoff: datetime) -> dict[str, dict]:
    """class -> {pages: [page titles], tasks: [task titles]}

    Only ACTIVE classes survive: at least one task dated within
    [today - 7d, today + 60d], or the class is a configured OneNote
    notebook.  Stale summer/last-year courses must not pollute proposals.
    """
    from config import get_setting

    # Only UPCOMING material qualifies a class as active for study
    # opportunities — a due date in the past (even yesterday) is not a
    # "study opportunity" the digest should surface. The +60d horizon
    # catches the next major test/assignment in each class.
    window_start = datetime.now().strftime("%Y-%m-%d")
    window_end = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
    notebook_names = [n.strip().lower() for n in
                      (get_setting("ONENOTE_NOTEBOOKS", "") or "").split(",") if n.strip()]
    classes_active: set[str] = set()
    # Soonest upcoming (within the active window) due date per class, used to
    # rank topics when the digest must show a limited number of buttons.
    next_due: dict[str, str] = {}
    classes: dict[str, dict] = defaultdict(lambda: {"pages": [], "tasks": []})

    canvas = _load_json(cache_dir / "canvas_page_extractions.json") or {}
    course_names = _load_course_names(cache_dir)
    for page_key, tasks in canvas.items():
        if not isinstance(tasks, list):
            continue
        course_id = str(page_key).split("/")[0].strip().split("::")[0].strip()
        course = course_names.get(course_id) or (
            f"Canvas course {course_id[:8]}" if _HASH_KEY_RE.match(course_id) else course_id
        )
        if not course:
            continue
        title = str(page_key).split("/")[-1]
        for item in tasks:
            if not isinstance(item, dict):
                continue
            due = str(item.get("due_date") or "")[:10]
            if window_start <= due <= window_end:
                classes_active.add(course)
                if next_due.get(course, "9999-99-99") > due:
                    next_due[course] = due
            if item.get("title"):
                classes[course]["tasks"].append(str(item["title"]))
        if not _HASH_KEY_RE.match(title):
            classes[course]["pages"].append(title)

    onenote = _load_json(cache_dir / "onenote_page_extractions.json") or {}
    for page_key, tasks in onenote.items():
        if not isinstance(tasks, list):
            continue
        parts = str(page_key).split("/")
        notebook = parts[0].strip().split("::")[0].strip() if parts else ""
        if notebook:
            classes[notebook]["pages"].append(parts[-1] if len(parts) > 2 else page_key)
            classes_active.add(notebook)
        for item in tasks:
            if isinstance(item, dict) and item.get("title"):
                classes[notebook]["tasks"].append(str(item["title"]))
                due = str(item.get("due_date") or "")[:10]
                if window_start <= due <= window_end and next_due.get(notebook, "9999-99-99") > due:
                    next_due[notebook] = due

    # Drop classes with no real material; keep only currently-active ones
    # (plus configured OneNote notebooks, which are always current).
    keep = {}
    for cls, data in classes.items():
        if len(" ".join(data["pages"] + data["tasks"]).strip()) < _MIN_MATERIAL_CHARS:
            continue
        if cls.lower() in notebook_names or cls in classes_active:
            data["active"] = True
            data["next_due"] = next_due.get(cls, "")
            keep[cls] = data
    return keep


def _deterministic_topics(data: dict, limit: int) -> list[str]:
    """Recent page/task titles, cleaned and deduped — no LLM needed."""
    seen: set[str] = set()
    out: list[str] = []
    for title in data["pages"] + data["tasks"]:
        if _HASH_KEY_RE.match(str(title).strip()):
            continue
        clean = _clean_title(title)
        low = clean.lower()
        if len(clean) < 6 or low in seen:
            continue
        if re.search(r"\b(advisement|syllabus|handbook|observe|signature)\b", low):
            continue
        if _is_past_date_title(clean):
            logger.debug("Dropped stale calendar page title: %r", clean)
            continue
        seen.add(low)
        out.append(clean)
        if len(out) >= limit:
            break
    return out


def _llm_refine(cls: str, topics: list[str]) -> list[str]:
    """Ask the online chain to name the topics as clean study subjects."""
    from scrapers.study_providers import generate_online

    prompt = (
        f"A student is learning the following material in their class \"{cls}\".\n"
        "Material page/task titles:\n" + "\n".join(f"- {t}" for t in topics) +
        "\n\nName the 2-4 specific academic topics/study subjects this material "
        "covers (e.g. 'Limits and Continuity', 'Cell Structure'). "
        "Respond ONLY with a JSON array of strings."
    )
    raw, provider = generate_online(prompt, max_tokens=300, timeout=120)
    if not raw:
        logger.info("Topic refinement unavailable (provider=%s); using deterministic topics", provider)
        return []
    import json as _json

    try:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        parsed = _json.loads(match.group(0)) if match else []
        refined = [_clean_title(t) for t in parsed if isinstance(t, str)]
        return [t for t in refined if len(t) >= 4][:4]
    except (ValueError, TypeError):
        logger.debug("Topic refinement output unparseable")
        return []


def discover_topics_per_class(
    cache_dir: Path | None = None,
    max_topics_per_class: int = 3,
    use_online_refine: bool = True,
    max_total_topics: int = 12,
) -> dict[str, list[str]]:
    """class name -> list of study topics, grounded in that class's material.

    ``max_total_topics`` caps the combined button list so the digest message
    stays under Telegram's size limit. Classes with the soonest upcoming dated
    task get priority; within the budget every shown class gets its first
    topic before any class gets a second (round-robin).
    """
    from config import CACHE_DIR

    cache_dir = Path(cache_dir or CACHE_DIR)
    material = _material_from_caches(cache_dir, datetime.now() - timedelta(days=400))
    if not material:
        logger.warning("Topic discovery found no per-class material in cache")
        return {}

    deterministic: dict[str, list[str]] = {}
    for cls, data in material.items():
        topics = _deterministic_topics(data, max_topics_per_class * 2)
        if topics:
            deterministic[cls] = topics

    if not deterministic:
        return {}

    refined: dict[str, list[str]] = {}
    if use_online_refine:
        refined = _llm_refine_batch(deterministic)

    # Priority order: classes with the SOONEST upcoming dated task first
    # (an explicit deadline is the strongest urgency signal), ties by name.
    rows: list[tuple[bool, str, str, str, list[str]]] = []
    for cls, deterministic_topics in deterministic.items():
        online = [t for t in refined.get(cls, []) if t]
        merged = online + [t for t in deterministic_topics
                           if t.lower() not in {o.lower() for o in online}]
        topics = merged[:max_topics_per_class]
        if not topics:
            continue
        display = _LAST_LABELS.get(cls, cls)
        meta = material.get(cls, {}) or {}
        due = str(meta.get("next_due") or "")
        has_due = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", due))
        rows.append((has_due, due, display.lower(), display, topics))
        logger.info("Topics for %s: %s", display, topics)

    if not rows:
        return {}

    max_total_topics = max(1, int(max_total_topics))
    rows.sort(key=lambda r: (not r[0], r[1], r[2]))
    total_before = sum(len(r[4]) for r in rows)

    final: dict[str, list[str]] = {}
    remaining = max_total_topics

    def _one_seat() -> None:
        """Every shown class gets its first topic before any gets a second."""
        nonlocal remaining
        for _, _, _, display, topics in rows:
            if remaining <= 0:
                return
            if display in final:
                continue
            final[display] = [topics[0]]
            remaining -= 1

    def _deepen() -> None:
        """Fill leftover budget by deepening classes in priority order."""
        nonlocal remaining
        for _, _, _, display, topics in rows:
            if remaining <= 0:
                return
            if display not in final:
                continue
            for t in topics[1:]:
                if remaining <= 0:
                    return
                final[display].append(t)
                remaining -= 1

    _one_seat()
    _deepen()

    shown = sum(len(v) for v in final.values())
    if shown < total_before:
        logger.info(
            "Topic discovery capped: %d/%d classes shown, %d/%d topics "
            "(max_total_topics=%d)\n%s",
            len(final), len(rows), shown, total_before, max_total_topics,
            "\n".join(f"- {k}: {', '.join(v)}" for k, v in final.items()),
        )
    return final


def _llm_refine_batch(classes_material: dict[str, list[str]]) -> dict[str, list[str]]:
    """One online call naming topics for every class at once."""
    from scrapers.study_providers import generate_online

    blocks = []
    for cls, topics in classes_material.items():
        bullet_list = "\n".join(f"- {t}" for t in topics[:8])
        blocks.append(f'CLASS: "{cls}"\n{bullet_list}')
    joined_blocks = "\n\n".join(blocks)
    prompt = (
        "A student is taking the classes below. For EACH class: infer the class's real "
        "subject name (label, e.g. 'AP Computer Science', 'AP Biology') and name 2-3 "
        "specific academic topics/study subjects the material covers.\n"
        "Respond ONLY with a JSON object mapping each class's material key to an object "
        "with keys \"label\" (inferred class name) and \"topics\" (array of 2-3 topic "
        "strings). Use the material keys exactly as given.\n\n"
        + joined_blocks
    )
    raw, provider = generate_online(prompt, max_tokens=800, timeout=240)
    if not raw:
        logger.info("Batched topic refinement unavailable (provider=%s)", provider)
        return {}
    import json as _json

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = _json.loads(match.group(0)) if match else {}
        out: dict[str, list[str]] = {}
        labels: dict[str, str] = {}
        for key, value in parsed.items():
            match_cls = next((k for k in classes_material if k.strip() == str(key).strip()), None)
            if match_cls is None:
                continue
            if isinstance(value, dict):
                label = _clean_title(value.get("label") or "")
                topics = value.get("topics") or []
            elif isinstance(value, list):
                label, topics = "", value
            else:
                continue
            clean = [_clean_title(t) for t in topics if isinstance(t, str) and len(t) >= 4][:4]
            if clean:
                out[match_cls] = clean
                if label:
                    labels[match_cls] = label
        _LAST_LABELS.update(labels)
        logger.info("Batched refinement served %d/%d classes via %s",
                    len(out), len(classes_material), provider)
        return out
    except (ValueError, TypeError):
        logger.debug("Batched refinement output unparseable")
        return {}
