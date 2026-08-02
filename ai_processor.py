"""
ai_processor.py - Runs one local inference request per source, saves results to text files,
then assembles the final digest and task list from those files.

REFACTORED: Uses llm_router for unified OpenRouter calls and llm_cost_log for tracking.
Private source processing is local-only; cloud routing is not permitted here.
"""

import asyncio
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:
    aiohttp = None  # optional: only needed for Pi classifier
logger = logging.getLogger(__name__)

# Use unified config
from config import CACHE_DIR as CONFIG_CACHE_DIR, LATEST_DIGEST_FILE
import config
BOT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = Path(CONFIG_CACHE_DIR)

# Module-level lock for thread-safe file writes (shared across ThreadPoolExecutor workers)
_write_lock = threading.Lock()

# ── Orange Pi 5 Classifier Integration ────────────────────────────────────────
# Offloads batch classification to the Pi's 8 cores (qwen2:0.5b, 4 concurrent
# workers) so the main server stays free for heavier local inference.

PI_CLASSIFIER_URL = config.PI_CLASSIFIER_URL


async def pi_classify_batch(items: list[dict]) -> list[dict] | None:
    """
    Send batch classification to Orange Pi 5 pipeline.

    Each item: {"id": str, "source": str, "text": str}
    Returns: list of classification results, or None if Pi is unreachable.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=60, connect=5)
        headers = {"Authorization": f"Bearer {config.PI_CLASSIFIER_TOKEN}"} if config.PI_CLASSIFIER_TOKEN else None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{PI_CLASSIFIER_URL}/classify",
                json=items,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Pi classifier returned HTTP {resp.status}")
                return None
    except Exception as exc:
        logger.info("Pi classifier unavailable; using local processing (%s).", type(exc).__name__)
        return None


def pi_classify_sync(items: list[dict]) -> list[dict] | None:
    """Synchronous wrapper for ThreadPoolExecutor."""
    try:
        return asyncio.run(pi_classify_batch(items))
    except Exception as e:
        logger.warning(f"Pi classifier sync call failed: {e}")
        return None


# ── Per-source prompts ────────────────────────────────────────────────────────


DIGEST_ASSEMBLY_PROMPT = (
    "You are generating Sanel's Telegram briefing from the supplied source summaries. "
    "Produce the finished briefing directly; do not describe how to make one.\n\n"
    "Rules:\n"
    "- Never apologize, refuse, say you cannot help, or offer a hypothetical summary.\n"
    "- Start with ⚡ **Needs attention** only when there is an actionable deadline, test, or request.\n"
    "- Then use short emoji section headers only for sources with useful new information: 📚 Canvas, 🏫 Google Classroom, 📢 Announcements, 📧 Gmail, 💬 GroupMe.\n"
    "- Use at most 3 bullets per section. Summarize announcements; never paste a raw wall of text.\n"
    "- Exclude 'no updates', stale, informational-only, and error messages.\n"
    "- If nothing needs attention, return exactly: ✅ All caught up — no new actionable updates.\n"
    "- Return Markdown followed by the two required JSON lines below. Do not add commentary after them.\n\n"
    "Summaries:\n{summaries}\n\n"
    "At the end, return these two JSON lines:\n"
    "1. A JSON list of specific upcoming subjects/topics the user has tests, quizzes, or heavy assignments for, in this exact format:\n"
    "STUDY_TOPICS_JSON:[\"Calculus Limits\", \"Photosynthesis\"]\n"
    "2. A JSON list of actionable tasks in this exact format:\n"
    "TASKS_JSON:[{{\"id\":\"...\",\"title\":\"...\",\"source\":\"...\",\"course\":null,\"url\":null,\"due_date\":null,\"priority\":\"medium\",\"status\":\"Not started\",\"start_value\":0,\"end_value\":100}}]\n"
    "Only create a task for work the user must act on. Never turn old, expired, completed, "
    "informational, attendance, or announcement-only items into tasks. Preserve an assignment Link as url and "
    "the bracketed Canvas course name as course when present.\n"
    "Priority guide: high = overdue, due within 3 days, a test, or a major submission; "
    "medium = due within 7 days; low = later, optional, club/informational, or no due date. "
    "CRITICAL: If you cannot confidently determine the 'priority', 'status', 'start_value', or 'end_value' from the text, set that specific field to 'unknown'."
)


MODEL_FAILURE_MARKERS = (
    "local inference unavailable",
    "cloud fallback disabled",
    "all models failed",
    "cannot assemble",
    "can't assemble",
    "i can't assemble",
    "i'm sorry, but i can't",
    "i am sorry, but i can't",
)


def _is_unusable_model_output(output: str | None) -> bool:
    """Recognize failures/refusals that must never reach a Telegram digest."""
    if not output or not output.strip():
        return True
    normalized = output.lower()
    return any(marker in normalized for marker in MODEL_FAILURE_MARKERS)


# ── Structured-payload extraction ─────────────────────────────────────────────
# The digest prompt asks for two bare marker lines (``TASKS_JSON:[...]``).  Small
# local models routinely answer with an equivalent but differently-shaped
# payload: a fenced ``json`` block, or an object whose *key* is the marker
# (``{"TASKS_JSON": [...]}``).  A quote between the marker and the colon defeats
# a naive ``MARKER:`` search, so the tasks were silently dropped and nothing
# reached Notion.  Accept every shape that carries the same meaning.

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """Return fenced block contents concatenated with the surrounding text."""
    if "```" not in text:
        return text
    inner = "\n".join(match.strip() for match in _FENCE_RE.findall(text))
    return f"{_FENCE_RE.sub(' ', text)}\n{inner}" if inner else text


def _balanced_slice(text: str, start: int) -> str | None:
    """Return the complete JSON array/object beginning at ``start``.

    A regex cannot match nested brackets, and task payloads contain nested
    objects, so scan for the balancing delimiter while ignoring brackets that
    appear inside string literals.
    """
    opener = text[start]
    closer = {"[": "]", "{": "}"}.get(opener)
    if closer is None:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _extract_marker_payload(text: str, marker: str) -> tuple[Any, str] | None:
    """Find ``marker``'s JSON payload in any shape the model emits.

    Returns ``(parsed_payload, raw_substring)`` so the caller can both use the
    data and strip the machine-readable part out of the human-facing digest.
    """
    if not text or marker not in text:
        return None
    haystack = _strip_code_fences(text)

    # Accept ``MARKER:``, ``"MARKER":``, ``'MARKER' :``, and markdown-emphasised
    # ``**MARKER:**`` alike — the 7B model bolds the marker, the 0.5B quotes it.
    pattern = re.compile(rf"[*_`\"']*{re.escape(marker)}[*_`\"']*\s*:", re.IGNORECASE)
    for match in pattern.finditer(haystack):
        cursor = match.end()
        # Step over trailing emphasis/whitespace between the colon and the payload
        # (``**TASKS_JSON:**[...]`` puts the closing asterisks after the colon).
        while cursor < len(haystack) and haystack[cursor] in " \t\r\n*_`":
            cursor += 1
        if cursor >= len(haystack) or haystack[cursor] not in "[{":
            continue
        raw = _balanced_slice(haystack, cursor)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        # ``{"TASKS_JSON": [...]}`` parsed from the outer object yields a dict
        # that still wraps the list under the marker key.
        if isinstance(parsed, dict) and marker in parsed:
            parsed = parsed[marker]
        return parsed, raw
    return None


# Small models rename fields.  Map their vocabulary onto the schema Notion needs
# rather than discarding an otherwise valid task.
_TASK_KEY_ALIASES = {
    "name": "title",
    "task": "title",
    "task_name": "title",
    "summary": "title",
    "due": "due_date",
    "due_at": "due_date",
    "deadline": "due_date",
    "link": "url",
    "class": "course",
    "subject": "course",
    "type": "task_type",
}

_ALLOWED_TASK_KEYS = {
    "id", "title", "source", "course", "url", "due_date",
    "priority", "status", "task_type", "start_value", "end_value",
}


def _normalize_task(raw: Any, index: int) -> dict | None:
    """Coerce one model-produced task into the shape ``add_task_to_notion`` wants."""
    if isinstance(raw, str):
        title = raw.strip()
        return {"id": f"llm:{index}", "title": title, "priority": "unknown",
                "status": "Not started"} if title else None
    if not isinstance(raw, dict):
        return None

    task: dict[str, Any] = {}
    for key, value in raw.items():
        canonical = _TASK_KEY_ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        if canonical in _ALLOWED_TASK_KEYS and canonical not in task:
            task[canonical] = value

    title = str(task.get("title") or "").strip()
    if not title:
        return None
    task["title"] = title
    task.setdefault("id", f"llm:{index}")
    task.setdefault("priority", "unknown")
    task.setdefault("status", "Not started")

    # A due date of "No due date"/"TBD"/null must not become a literal string,
    # because the calendar and Notion both branch on its presence.
    due = task.get("due_date")
    if isinstance(due, str):
        cleaned = due.strip()
        if cleaned.lower() in {"", "none", "null", "n/a", "tbd", "no due date", "unknown"}:
            task["due_date"] = None
        else:
            # Trim a time component so downstream date parsing stays simple.
            iso = re.match(r"(\d{4}-\d{2}-\d{2})", cleaned)
            task["due_date"] = iso.group(1) if iso else cleaned
    return task


def _parse_llm_tasks(text: str) -> tuple[list[dict], str]:
    """Extract the task list from a digest, returning ``(tasks, cleaned_digest)``."""
    found = _extract_marker_payload(text, "TASKS_JSON")
    if not found:
        return [], text
    payload, raw = found
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return [], text

    tasks: list[dict] = []
    for index, item in enumerate(payload):
        normalized = _normalize_task(item, index)
        if normalized:
            tasks.append(normalized)
    return tasks, _remove_payload(text, "TASKS_JSON", raw)


def _parse_llm_topics(text: str) -> tuple[list[str], str]:
    """Extract study topics from a digest, returning ``(topics, cleaned_digest)``."""
    found = _extract_marker_payload(text, "STUDY_TOPICS_JSON")
    if not found:
        return [], text
    payload, raw = found
    if isinstance(payload, str):
        payload = [payload]
    if not isinstance(payload, list):
        return [], text
    topics = [str(item).strip() for item in payload if str(item).strip()]
    return topics, _remove_payload(text, "STUDY_TOPICS_JSON", raw)


def _remove_payload(text: str, marker: str, raw: str) -> str:
    """Strip a marker and its payload from the human-facing digest text."""
    cleaned = text.replace(raw, "")
    cleaned = re.sub(rf"[*_`\"']*{re.escape(marker)}[*_`\"']*\s*:\s*[*_`]*,?", "", cleaned, flags=re.IGNORECASE)
    # Leave no empty fence or dangling brace behind once the payload is gone.
    cleaned = re.sub(r"```(?:json|JSON)?\s*\{?\s*\}?\s*```", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ── agy helper ────────────────────────────────────────────────────────────────

def call_agy(prompt: str, timeout: int = 180, model: str = "flash") -> str:
    """Deprecated compatibility wrapper for local-only digest inference."""
    return _local_inference(prompt, timeout=timeout, max_tokens=1_500)


def _local_inference(prompt: str, *, timeout: int, max_tokens: int) -> str:
    """Run private digest processing on local inference only, with typed failure."""
    from llm_router import call_local_rpc_result

    result = call_local_rpc_result(
        prompt=prompt,
        max_tokens=max_tokens,
        timeout=min(max(1, int(timeout)), config.RPC_INFERENCE_TIMEOUT),
        temperature=0.0,
        allow_cloud=False,
    )
    if result.ok:
        return result.text
    logger.warning("Local inference unavailable for digest processing (%s).", result.status.value)
    return ""


# ── Per-source processing ─────────────────────────────────────────────────────

# ── Deterministic source compaction ───────────────────────────────────────────
# High-signal sources bypass LLM summarization (skip_llm_filter=True) so a weak
# model cannot drop a due date.  The cost is that their raw text lands in the
# digest prompt verbatim: Classroom announcements alone were 54% of the prompt,
# and prompt evaluation runs at ~15 tok/s on the RPC cluster, so every wasted
# character is paid for twice (once here, once in the assembly call).
#
# Compact them deterministically instead — no inference, so nothing can be lost
# to a model refusal or timeout, and the result is byte-stable across runs.

# ``[Course Name]: text`` — the scraper re-states the course on every line.
_PREFIXED_ITEM_RE = re.compile(r"^\[([^\]]{1,80})\]:\s*(.*)$")

# Conversational openers that carry no scheduling information.
_BOILERPLATE_RE = re.compile(
    r"^(?:hello(?:\s+all)?|hi(?:\s+all)?|hey(?:\s+all)?|good\s+(?:morning|afternoon|evening))"
    r"[,!.\s]+",
    re.IGNORECASE,
)


def _compact_source_text(text: str, *, per_item_chars: int = 220, total_chars: int = 2_000) -> str:
    """Shrink a verbatim source dump without an LLM call.

    Hoists a course prefix repeated on every line into a single header, collapses
    runs of whitespace, trims greeting boilerplate, and applies per-item and
    total budgets.  Returns ``text`` unchanged when it is already small enough.

    When the total budget is exceeded the per-item budget is tightened first so
    every item survives in shortened form.  Items are only dropped as a last
    resort, and the newest are kept: the scrapers emit oldest-first, so trimming
    the tail would discard the most recent announcement.
    """
    if not text or len(text) <= total_chars:
        return text

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return text

    header = ""
    if lines and not _PREFIXED_ITEM_RE.match(lines[0]):
        # First line is a section banner ("📢 **... Announcements ...**"), not an item.
        header = lines[0]
        lines = lines[1:]

    parsed: list[tuple[str, str]] = []
    for line in lines:
        match = _PREFIXED_ITEM_RE.match(line)
        parsed.append((match.group(1).strip(), match.group(2).strip()) if match else ("", line))

    # Hoist the prefix only when every item shares it; otherwise keep prefixes so
    # multi-course output stays attributable.
    prefixes = {prefix for prefix, _ in parsed if prefix}
    hoist = len(prefixes) == 1 and all(prefix for prefix, _ in parsed)
    if hoist:
        only = prefixes.pop()
        header = f"{header} [{only}]".strip() if header else f"[{only}]"

    # Normalize once; only the per-item truncation depends on the budget.
    cleaned: list[tuple[str, str]] = []
    for prefix, body in parsed:
        body = re.sub(r"\s+", " ", body)
        body = _BOILERPLATE_RE.sub("", body).strip()
        if body:
            cleaned.append((prefix, body))
    if not cleaned:
        return text

    def render(budget: int) -> str:
        items = []
        for prefix, body in cleaned:
            if len(body) > budget:
                window = body[:budget]
                cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
                if cut < budget // 2:
                    cut = window.rfind(" ")
                body = (window[:cut] if cut > 0 else window).rstrip(" ,;:") + "…"
            items.append(f"- {body}" if hoist else f"- [{prefix}] {body}" if prefix else f"- {body}")
        return "\n".join(([header] if header else []) + items)

    rendered = render(per_item_chars)

    # Tighten per-item budget before sacrificing whole items.
    if len(rendered) > total_chars:
        for budget in (180, 150, 120, 100, 80):
            rendered = render(budget)
            if len(rendered) <= total_chars:
                break

    # Still too long: keep the newest items (end of list) and note the omission.
    if len(rendered) > total_chars:
        items = render(80).splitlines()
        body_items = items[1:] if header else items
        kept: list[str] = []
        budget = total_chars - (len(header) + 1 if header else 0) - 40
        for item in reversed(body_items):
            if budget - (len(item) + 1) < 0:
                break
            kept.insert(0, item)
            budget -= len(item) + 1
        dropped = len(body_items) - len(kept)
        if dropped > 0:
            kept.insert(0, f"- (+{dropped} older announcement(s) omitted)")
        rendered = "\n".join(([header] if header else []) + kept)

    # Never return something larger than what we started with.
    return rendered if len(rendered) < len(text) else text


def process_source(name: str, data: str, skip_llm_filter: bool = False, force_reprocess: bool = False) -> str:
    """Summarize one private source locally and save a bounded cache entry."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_file = os.path.join(CACHE_DIR, f"{name}_summary.txt")

    if not data or data.strip() == "" or "not configured" in data.lower():
        summary = f"No {name} data available."
        with open(cache_file, "w") as f:
            f.write(summary)
        return summary

    # Content-hash caching: skip LLM processing if source unchanged
    if not force_reprocess:
        try:
            from utils import has_changed
            if not has_changed(name, data):
                try:
                    with open(cache_file, "r", encoding="utf-8") as f:
                        cached = f.read()
                    if cached and cached != f"No {name} data available.":
                        logger.info(f"Source {name} unchanged — using cached summary ({len(cached)} chars)")
                        return cached
                except Exception as e:
                    logger.debug(f"cache read fell through, regenerating {name}: %r", e)
        except ImportError:
            pass  # utils not available, skip caching

    # ── Stage 1: Try Orange Pi classifier first to avoid unnecessary inference. ──
    if not skip_llm_filter:
        pi_result = pi_classify_sync([{"id": "1", "source": name, "text": data[:2000]}])
        if pi_result and len(pi_result) > 0:
            classification = pi_result[0].get("classification", "UNSURE")
            confidence = pi_result[0].get("confidence", 0.0)
            logger.info(f"Pi classifier: {name} → {classification} ({confidence:.0%})")

            if classification == "NOISE" and confidence >= 0.8:
                summary = f"No urgent {name} updates."
                with open(cache_file, "w") as f:
                    f.write(summary)
                logger.info("Pi classifier marked %s as noise; skipping local summarization.", name)
                return summary

    if skip_llm_filter:
        # High-signal source: no LLM gate, so a weak model cannot drop a due
        # date.  Compact deterministically rather than pasting the raw dump into
        # the digest prompt — prompt eval is the dominant cost on the cluster.
        compacted = _compact_source_text(data)
        if len(compacted) < len(data):
            logger.info(
                "Bypassing classification for high-signal source %s — compacted %d -> %d chars.",
                name, len(data), len(compacted),
            )
        else:
            logger.info(
                f"Bypassing classification for high-signal source {name} — passing full raw data ({len(data)} chars)."
            )
        summary = compacted
    else:
        # Lightweight classification via agy flash (replaces old Qwen2 0.5B → Llama → agy 3-step chain)
        prompt = (
            f"Read the following {name} data. If it contains ANY useful info, summarize it concisely. "
            f"If it's empty/useless, reply exactly: NO_IMPORTANT_UPDATES\n\n"
            f"DATA:\n{data[:8000]}"
        )

        # Inject user's dynamic learning rules
        rules_file = os.path.join(BOT_DIR, "learning_rules.txt")
        if os.path.exists(rules_file):
            try:
                with open(rules_file, "r") as f:
                    rules = f.read().strip()
                if rules:
                    prompt += f"\n\nUSER'S CUSTOM RULES (MUST FOLLOW):\n{rules}\n"
            except Exception as e:
                logger.debug("rules_file unreadable, proceeding without: %r", e)

        prompt += "\n\nIf you see a completely new type of item you're unsure about, reply: [ASK_USER] description"

        logger.info("Calling local inference for %s classification (%d chars).", name, len(prompt))
        response = _local_inference(prompt, timeout=60, max_tokens=1_500)

        if _is_unusable_model_output(response):
            # Do not cache an error string such as "Local inference
            # unavailable" as the source summary.  Raw source data is still
            # better than losing a due date; assemble_digest has a safe
            # deterministic formatter if its own model call is unavailable.
            logger.warning("LLM summary for %s was unavailable; preserving source data for fallback.", name)
            summary = data[:8000]
        elif "NO_IMPORTANT_UPDATES" in response.upper():
            summary = f"No urgent {name} updates."
        elif "[ASK_USER]" in response:
            summary = f"{response}\n\nRAW DATA:\n{data}"
        else:
            summary = response

    with open(cache_file, "w") as f:
        f.write(summary)

    try:
        from utils import mark_processed as _mark_processed
        _mark_processed(name, data)
    except ImportError:
        pass

    logger.info(f"Saved {name} summary to {cache_file}")
    return summary


# ── Final assembly ────────────────────────────────────────────────────────────

_EMPTY_SOURCE_MARKERS = (
    "no recent ",
    "no urgent ",
    "no active ",
    "no unread ",
    "no new ",
    "no important ",
    "no canvas data",
    "not configured",
    "error fetching",
    "error connecting",
    "local inference unavailable",
    "cloud fallback disabled",
)

_SOURCE_LABELS = {
    "canvas": "📚 **Canvas**",
    "classroom": "🏫 **Google Classroom**",
    "classroom_announcements": "📢 **Announcements**",
    "gmail": "📧 **Gmail**",
    "groupme": "💬 **GroupMe**",
    "gdocs": "📄 **Google Docs**",
}


def _compact_digest_lines(text: str, limit: int = 3) -> list[str]:
    """Turn raw scraper output into a few safe, readable Telegram bullets."""
    import re

    lines: list[str] = []
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        normalized = line.lower().lstrip("-• ")
        if any(marker in normalized for marker in _EMPTY_SOURCE_MARKERS):
            continue
        # Drop source headings—the digest supplies consistent headings itself.
        if normalized.startswith(("canvas assignments", "google classroom assignments", "recent google docs")):
            continue
        line = line.lstrip("-• ")
        if len(line) > 220:
            line = line[:217].rstrip() + "…"
        if line not in lines:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _fallback_tasks(summaries: dict[str, str]) -> list[dict]:
    """Recover dated Canvas/Classroom tasks when the digest model is offline."""
    import re
    from datetime import date, timedelta

    task_sources = {"canvas": "Canvas", "classroom": "Google Classroom"}
    due_pattern = re.compile(r"\bDue:\s*(\d{4}-\d{2}-\d{2})")
    course_pattern = re.compile(r"^\s*(?:[-•]\s*)?\[([^\]]+)\]\s*(.*)$")
    tasks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    overdue_cutoff = date.today() - timedelta(days=7)

    for source_key, source_name in task_sources.items():
        for line in (summaries.get(source_key) or "").splitlines():
            due_match = due_pattern.search(line)
            course_match = course_pattern.match(line)
            if not due_match or not course_match:
                continue
            try:
                due_date = date.fromisoformat(due_match.group(1))
            except ValueError:
                continue
            if due_date < overdue_cutoff:
                continue
            course, title = course_match.groups()
            title = title[:due_match.start() - course_match.start(2)].strip()
            title = title.rstrip(" -—(").strip()
            if not title or (title, due_date.isoformat()) in seen:
                continue
            seen.add((title, due_date.isoformat()))
            tasks.append({
                "id": f"fallback:{source_key}:{len(tasks)}",
                "title": title,
                "source": source_name,
                "course": course,
                "url": None,
                "due_date": due_date.isoformat(),
                "priority": "unknown",
                "status": "Not started",
                "start_value": 0,
                "end_value": 100,
            })
    return tasks


def _deterministic_digest(summaries: dict[str, str]) -> tuple[str, list[dict]]:
    """Provide a compact, useful briefing without relying on an LLM."""
    tasks = _fallback_tasks(summaries)
    sections: list[str] = []
    if tasks:
        action_lines = []
        for task in tasks[:5]:
            action_lines.append(f"• {task['title']} — due {task['due_date']}")
        sections.append("⚡ **Needs attention**\n" + "\n".join(action_lines))

    for source_key, label in _SOURCE_LABELS.items():
        lines = _compact_digest_lines(summaries.get(source_key, ""))
        # Dated coursework is already shown in Needs attention.
        if source_key in {"canvas", "classroom"}:
            lines = [line for line in lines if "due:" not in line.lower()]
        if lines:
            sections.append(label + "\n" + "\n".join(f"• {line}" for line in lines))

    if not sections:
        return "✅ All caught up — no new actionable updates.", tasks
    return "\n\n".join(sections), tasks

def assemble_digest(summaries: dict) -> dict:
    """Assemble per-source summaries into a local digest and task list."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary_text = ""
    for name, text in summaries.items():
        summary_text += f"=== {name.upper()} ===\n{text}\n\n"

    # Save combined summaries to file for reference (APPEND so memory consolidation can read the whole day)
    # Use atomic append with lock to prevent interleaved writes from ThreadPoolExecutor
    combined_summaries_path = os.path.join(CACHE_DIR, "combined_summaries.txt")
    with _write_lock:
        with open(combined_summaries_path, "a", encoding="utf-8") as f:
            f.write(summary_text)
    # Rotate combined_summaries.txt to prevent unbounded growth
    from utils import rotate_file_if_needed
    from config import MAX_COMBINED_SUMMARIES_CHARS
    rotate_file_if_needed(Path(combined_summaries_path), MAX_COMBINED_SUMMARIES_CHARS)
        
    # Read and inject local OCR / photo extracts
    extracts_file = config.IMPORTANT_EXTRACTS_FILE
    if os.path.exists(extracts_file):
        try:
            with open(extracts_file, "r") as f:
                extracts = f.read().strip()
            if extracts:
                summary_text += f"\n=== LOCAL PHOTO / OFFLINE EXTRACTS ===\n{extracts}\n\n"
            # Clear the file now that it's in the digest
            with open(extracts_file, "w") as f:
                f.write("")
        except Exception as e:
            logger.error(f"Failed to read extracts: {e}")

    prompt = DIGEST_ASSEMBLY_PROMPT.format(summaries=summary_text)
    logger.info("Assembling final digest via local inference...")
    output = _local_inference(prompt, timeout=600, max_tokens=config.DIGEST_MAX_TOKENS)

    # Split tasks JSON and topics JSON from the digest text
    tasks = []
    topics = []
    if _is_unusable_model_output(output):
        logger.warning("Digest model was unavailable or refused; using deterministic briefing fallback.")
        digest, tasks = _deterministic_digest(summaries)
    else:
        digest = output
        tasks, digest = _parse_llm_tasks(digest)
        topics, digest = _parse_llm_topics(digest)

        # A model that emitted no usable task payload is not the same as a model
        # that decided there is nothing to do: the marker being absent or
        # unparseable used to yield zero tasks silently, so nothing ever reached
        # Notion.  Fall back to deterministic extraction, which reads due dates
        # straight out of the source summaries.
        if not tasks and "TASKS_JSON" not in output:
            fallback_digest, fallback_tasks = _deterministic_digest(summaries)
            if fallback_tasks:
                logger.warning(
                    "Digest model returned no TASKS_JSON marker; recovered %d task(s) deterministically.",
                    len(fallback_tasks),
                )
                tasks = fallback_tasks
                if _is_unusable_model_output(digest):
                    digest = fallback_digest
        elif not tasks:
            logger.warning("TASKS_JSON present but yielded no usable tasks; check the model output shape.")

    import re as _re

    # ── Deduplication: bullet-level comparison using persistent hash set ────
    # Uses seen_bullets.json to track ALL bullets ever seen, preventing
    # oscillation where old bullets are forgotten and re-notified.
    import re as _re
    previous_digest_path = LATEST_DIGEST_FILE
    seen_bullets_path = CACHE_DIR / "seen_bullets.json"
    seen_bullets: set = set()
    try:
        if seen_bullets_path.exists():
            seen_bullets = set(json.loads(seen_bullets_path.read_text()))
    except Exception as e:
        logger.debug("seen_bullets unreadable, starting fresh: %r", e)

    if seen_bullets:
        kept = []
        new_bullet_count = 0
        for line in digest.split("\n"):
            stripped = line.strip()
            if stripped.startswith(("•", "-", "✅", "📎", "▶️")):
                normalized = _re.sub(r'[^\w\s]', '', stripped).strip().lower()
                if normalized not in seen_bullets:
                    kept.append(line)
                    seen_bullets.add(normalized)
                    new_bullet_count += 1
                # else: duplicate bullet, skip
            else:
                kept.append(line)  # keep headers, blank lines, etc.

        if new_bullet_count == 0:
            digest = "✅ Nothing new since the last digest — all caught up!"
            logger.info("Deduplication: no new updates found (bullet-level).")
        else:
            digest = "\n".join(kept)
            logger.info(f"Deduplication: kept {new_bullet_count} new bullets, removed duplicates.")
    else:
        # First run: seed the persistent set with all current bullets
        for line in digest.split("\n"):
            stripped = line.strip()
            if stripped.startswith(("•", "-", "✅", "📎", "▶️")):
                normalized = _re.sub(r'[^\w\s]', '', stripped).strip().lower()
                seen_bullets.add(normalized)

    # Persist seen bullets (cap at 5000 to prevent unbounded growth)
    try:
        bullet_list = list(seen_bullets)
        if len(bullet_list) > 5000:
            bullet_list = bullet_list[-5000:]
        seen_bullets_path.write_text(json.dumps(bullet_list))
        logger.info(f"Persisted {len(bullet_list)} seen bullets to {seen_bullets_path}")
    except Exception as e:
        logger.error(f"Failed to persist seen bullets: {e}")

    # Save deduped digest to latest_digest.txt for display
    try:
        with open(previous_digest_path, "w") as f:
            f.write(digest)
    except Exception as e:
        logger.error(f"Failed to save digest: {e}")

    return {"tasks": tasks, "digest": digest, "topics": topics}


# ── Main entry point ──────────────────────────────────────────────────────────

def process_all_sources(canvas_data: str, classroom_data: str, gmail_data: str, groupme_data: str, classroom_ann_data: str = "No recent announcements.", gdocs_data: str = "No recent docs.") -> dict:
    """Passes all raw data through the AI pipeline. Runs sources in parallel."""

    # 1. Summarize all 6 sources in parallel (independent I/O-bound work)
    import concurrent.futures
    sources = [
        ("canvas", canvas_data, True, False),
        ("classroom", classroom_data, True, False),
        ("classroom_announcements", classroom_ann_data, True, False),
        ("gmail", gmail_data, False, False),
        ("groupme", groupme_data, False, False),
        ("gdocs", gdocs_data, True, False),
    ]

    def _process_one(args):
        name, data, skip, force = args
        logger.info(f"Summarizing {name}...")
        return name, process_source(name, data, skip_llm_filter=skip, force_reprocess=force)

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for name, summary in pool.map(_process_one, sources):
            results[name] = summary

    # 2. Combine all summaries into one assembly block
    summaries = {
        "canvas": results["canvas"],
        "classroom": results["classroom"],
        "classroom_announcements": results["classroom_announcements"],
        "gmail": results["gmail"],
        "groupme": results["groupme"],
        "gdocs": results["gdocs"],
    }

    return assemble_digest(summaries)
