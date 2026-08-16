import os
import json
import logging

from activity_log import close_activity_log, log_event, log_llm_call, log_scrape, log_system, log_nightly, get_recent_events, format_events
from utils import scrub_pii
import time
import asyncio
import subprocess
import sys
import datetime
from zoneinfo import ZoneInfo
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
from bot.security import require_auth
from bot.commands import model_command, summary_command, bash_command, priority_command, ping_command, stats_command, backup_command, restore_command, correlations_command, classroom_pdfs_command, canvas_command, calendar_command, help_command, server_command, errors_command, handle_callback, dashboard_message
from bot.ui import begin_progress, edit_progress, escape_html, render_assistant_text, send_assistant_response

import config
import tempfile
from inline_keyboards import get_digest_topic_keyboard, get_photo_response_keyboard, get_task_actions_keyboard
from utils import correlate_items, enforce_all_rotations, create_backup
from voice_handler import transcribe_voice
from bot.runtime import _track_task, _cleanup_background_tasks


# ── Config ─────────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
# Kept solely for legacy helper compatibility.  It never reads an unrelated
# CLI transcript outside the application's private runtime root.
TRANSCRIPT_PATH = str(config.STATE_DIR / "legacy_transcript.jsonl")

# ── Data source toggle – Composio remains available for Google sources. ──
# Canvas always uses the local Firefox/ClassLink session in canvas_scraper.py.
USE_COMPOSIO = config.USE_COMPOSIO

def configure_logging() -> None:
    """Configure stdout logging during application startup, not import."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            level=logging.INFO,
        )
    # HTTPX logs full request URLs at INFO.  Telegram Bot API URLs contain the
    # bot token, so transport-level request logging must never reach journald.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)

# Ensure project root is on sys.path once (avoids repeated sys.path.append)
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)


# ── Transcript helpers ─────────────────────────────────────────────────────────

def get_last_step_index() -> int:
    """Return the highest step_index currently in the transcript."""
    last = -1
    try:
        with open(TRANSCRIPT_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    idx = obj.get("step_index", -1)
                    if idx > last:
                        last = idx
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return last


def get_new_responses(after_step: int) -> list[str]:
    """
    Return a list of completed PLANNER_RESPONSE content strings
    that appear after `after_step`.
    """
    responses = []
    try:
        with open(TRANSCRIPT_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    obj.get("step_index", -1) > after_step
                    and obj.get("source") == "MODEL"
                    and obj.get("type") == "PLANNER_RESPONSE"
                    and obj.get("status") == "DONE"
                ):
                    content = obj.get("content", "").strip()
                    if content:
                        responses.append(content)
    except FileNotFoundError:
        pass
    return responses

from bot.ai_bridge import detect_topic, send_to_antigravity_and_wait




# ── Background Automation ──────────────────────────────────────────────────────

from bot.state import load_state, save_state, update_state, is_sleep_window, get_hash

async def watchdog_check(context: ContextTypes.DEFAULT_TYPE):
    if watchdog_lock.locked():
        logger.warning("watchdog already running, skipping")
        return
    async with watchdog_lock:
        await _watchdog_impl(context)


async def _watchdog_impl(context: ContextTypes.DEFAULT_TYPE):
    """Runs every 30 mins to check for urgent anomalies using tiny local model Qwen2 0.5B."""
    if is_sleep_window(): return
    chat_id = context.job.chat_id
    state = load_state()
    # sys.path already set at module level
    from scrapers.canvas_scraper import get_all_canvas_data
    if USE_COMPOSIO:
        from scrapers.composio_fetcher import (
            get_unread_emails,
            get_classroom_assignments, get_classroom_announcements
        )
    else:
        from scrapers.google_scraper import get_unread_emails, get_classroom_assignments, get_classroom_announcements
    from scrapers.groupme_scraper import get_latest_messages

    logger.info("Watchdog: Scraping sources...")

    # ── Scrape each source independently so one failure doesn't kill the watchdog ──
    async def _safe_scrape(name, func, *args):
        try:
            scrape_timeout = 180 if name == "canvas" else 60
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args),
                timeout=scrape_timeout
            )
        except Exception as e:
            err_msg = str(e)[:120]
            # Suppress known noise: CSRF warnings from expired Google tokens
            if "CSRF" in err_msg or "mismatching_state" in err_msg:
                logger.info(f"Watchdog: Skipping {name} — Google token expired (run google_auth_setup.py to fix)")
            else:
                logger.warning(f"Watchdog: Scraper '{name}' failed: {err_msg}")
            return f"(⚠️ {name} unavailable: {err_msg})"

    canvas      = await _safe_scrape("canvas", get_all_canvas_data)
    classroom   = await _safe_scrape("classroom", get_classroom_assignments)
    classroom_ann = await _safe_scrape("classroom_ann", get_classroom_announcements)
    gmail       = await _safe_scrape("gmail", get_unread_emails)
    groupme     = await _safe_scrape("groupme", get_latest_messages, "102851186")

    raw_data = f"CANVAS:\n{canvas}\n\nCLASSROOM:\n{classroom}\n\nCLASSROOM ANNOUNCEMENTS:\n{classroom_ann}\n\nGMAIL:\n{gmail}\n\nGROUPME:\n{groupme}"

    import re

    # Match all attached files
    all_files = re.findall(r"📎\s+([^\(]+)\s*\((https://drive\.google\.com/file/d/([^/]+)/[^\)]+)\)", classroom)

    pending_queue_items: list[dict] = []

    for title, full_link, file_id in all_files:
        thash = get_hash("file_" + file_id)
        if thash not in state.get("seen_tasks", []):
            title = title.strip()
            if "A_MWF" in title:
                # Auto-read handwritten notes
                await context.bot.send_message(chat_id=chat_id, text=f"📝 Reading notes from {title[:120]} locally.")

                # Run it in a background thread to not block the event loop
                def _extract():
                    from scrapers.google_scraper import download_drive_file
                    from scrapers.extract_notes import transcribe_handwritten_pdf
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                        path = tmp.name
                    try:
                        if not download_drive_file(file_id, path):
                            return False
                        transcript = transcribe_handwritten_pdf(path)
                        if "Error:" in transcript:
                            return False
                        notes_file = config.COMBINED_SUMMARIES_FILE
                        notes_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with open(notes_file, "a", encoding="utf-8") as handle:
                            handle.write(f"\n--- DAILY NOTES ({title[:200]}) ---\n{transcript}\n")
                        return True
                    finally:
                        try:
                            os.unlink(path)
                        except OSError:
                            pass

                if await asyncio.to_thread(_extract):
                    update_state(lambda s, item_hash=thash: s.setdefault("seen_tasks", []).append(item_hash))
            else:
                pending_queue_items.append({"title": title[:200], "file_id": file_id, "source": "google_drive"})

    if pending_queue_items:
        from bot.storage import AtomicJSONStore

        added = 0
        def enqueue(queue):
            nonlocal added
            if not isinstance(queue, list):
                queue = []
            existing = {str(item.get("file_id")) for item in queue if isinstance(item, dict)}
            for item in pending_queue_items:
                if item["file_id"] not in existing:
                    queue.append(item)
                    existing.add(item["file_id"])
                    added += 1
            return queue

        AtomicJSONStore(config.NIGHTLY_QUEUE_FILE, list).update(enqueue)
        if added:
            update_state(lambda s: s.setdefault("seen_tasks", []).extend(
                get_hash("file_" + item["file_id"]) for item in pending_queue_items
            ))
            await context.bot.send_message(chat_id=chat_id, text=f"🌙 Queued {added} study document(s) for local overnight processing.")

    prompt = (
            "You are an urgent alert watchdog. Read the following recent school and email notifications.\n"
            "Look ONLY for critical anomalies or urgent updates (e.g., a sudden deadline extension, a direct message from a teacher, or an emergency alert).\n"
            "If you find something genuinely urgent, write a short 1-sentence warning about it.\n"
            "If there is nothing urgent, you MUST reply with exactly the word: NO_ALERT\n\n"
            f"DATA:\n{raw_data}"
        )
    try:
        from llm_router import call_local_rpc, call_openrouter
        from config import OR_FALLBACK_MODEL, OR_THIRD_MODEL

        # Try local RPC first (no rate limits, always available)
        result = await asyncio.to_thread(
            call_local_rpc,
            prompt=prompt,
            max_tokens=150,
            timeout=90,
            allow_cloud=False,
        )

        if result and "⚠️ Local inference unavailable" in result:
            result = "NO_ALERT"

        # Send alert regardless of which model produced the result
        # But filter out the "all models failed" fallback message
        if (result and "NO_ALERT" not in result and len(result) > 10
                and "All models failed" not in result):
            # Dedup: don't re-send the same urgent alert every 30 min.
            # Normalize (lowercase, collapse whitespace, drop punctuation) so
            # minor LLM phrasing drift on the same event still hashes equal.
            import re as _re
            normalized = _re.sub(r"[^a-z0-9 ]", "", result.lower())
            normalized = _re.sub(r"\s+", " ", normalized).strip()
            alert_hash = get_hash(normalized)

            should_send_alert = False

            def record_alert(state):
                nonlocal should_send_alert
                seen_alerts = state.setdefault("seen_alerts", [])
                if alert_hash in seen_alerts:
                    return
                seen_alerts.append(alert_hash)
                if len(seen_alerts) > 100:
                    state["seen_alerts"] = seen_alerts[-100:]
                should_send_alert = True

            update_state(record_alert)
            if not should_send_alert:
                logger.info("Watchdog: duplicate alert suppressed (already sent).")
            else:
                logger.info(f"Watchdog triggered: {result}")
                from utils import sanitize_markdown
                safe_result = sanitize_markdown(result)
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🚨 **WATCHDOG ALERT** 🚨\n\n{safe_result}",
                    parse_mode="Markdown"
                )
        elif result and "All models failed" in result:
            logger.warning("Watchdog: all LLM models failed (rate limited), skipping check")
        else:
            logger.info("Watchdog check clear (no alerts).")
    except Exception as e:
            logger.error(f"Watchdog Ollama error: {e}")

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    # Prevent overlapping executions if previous run takes >4 hours
    if digest_lock.locked():
        logger.warning("check_updates already running, skipping this tick")
        return

    async with digest_lock:
        await _check_updates_impl(context)


async def _check_updates_impl(context: ContextTypes.DEFAULT_TYPE):
    if is_sleep_window(): return
    chat_id = context.job.chat_id
    state = load_state()

    from scrapers.canvas_scraper import get_all_canvas_data
    if USE_COMPOSIO:
        from scrapers.composio_fetcher import (
            get_unread_emails,
            get_classroom_assignments, get_classroom_announcements,
            get_recent_google_docs
        )
    else:
        from scrapers.google_scraper import get_unread_emails, get_classroom_assignments, get_classroom_announcements, get_recent_google_docs
    from scrapers.groupme_scraper import get_latest_messages
    from ai_processor import process_all_sources
    from scrapers.notion_client import add_task_to_notion, determine_task_priority, priority_emoji

    logger.info("Background job: Scraping sources...")

    # ── Per-source error recovery: each scraper runs independently ─────────
    async def _safe_scrape(name, func, *args):
        try:
            scrape_timeout = 180 if name == "canvas" else 60
            return await asyncio.wait_for(
                asyncio.to_thread(func, *args),
                timeout=scrape_timeout
            )
        except Exception as e:
            logger.error(f"Scraper {name} failed: {e}")
            return f"Error fetching {name}: {e}"

    c = await _safe_scrape("canvas", get_all_canvas_data)
    cl = await _safe_scrape("classroom", get_classroom_assignments)
    cla = await _safe_scrape("classroom_ann", get_classroom_announcements)
    gm = await _safe_scrape("gmail", get_unread_emails)
    grp = await _safe_scrape("groupme", get_latest_messages, "102851186")
    gd = await _safe_scrape("gdocs", get_recent_google_docs)

    logger.info("Background job: Processing with AI...")
    try:
        ai_result = await asyncio.to_thread(process_all_sources, c, cl, gm, grp, cla, gd)
    except Exception as e:
        logger.error(f"AI processing failed: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "<b>I couldn’t refresh the background digest.</b>\n\n"
                "Your existing tasks and calendar entries were not changed. "
                "Open Today and retry when your connected sources are available."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    # Calendar writes are disabled by default. Once explicitly enabled, only
    # official Canvas/Classroom work is reconciled during this normal refresh.
    try:
        from scrapers.assignment_calendar import AssignmentCalendarService
        calendar_service = AssignmentCalendarService()
        if calendar_service.store.is_enabled():
            calendar_changes = await asyncio.to_thread(calendar_service.sync_official)
            logger.info("Assignment calendar reconciled %d official change(s)", calendar_changes)
    except Exception as exc:
        logger.warning("Assignment calendar reconciliation failed: %s", exc)

    # 1. Notion Tasks
    import difflib
    new_tasks = []

    # Migrate old hash state to list of strings (hashes won't fuzzy match but we'll store new ones as strings)
    seen_titles = state.setdefault("seen_tasks", [])

    for task in ai_result.get("tasks", []):
        task_title = task.get("title", "").strip().lower()
        if not task_title: continue

        # Fuzzy match against seen tasks
        is_duplicate = False
        for seen in seen_titles:
            # If it's a legacy MD5 hash (length 32, hex), SequenceMatcher will just give 0.0 which is fine
            similarity = difflib.SequenceMatcher(None, task_title, seen).ratio()
            if similarity > 0.8:
                is_duplicate = True
                break

        if not is_duplicate:
            new_tasks.append(task)


    if new_tasks:
        successful_tasks = []
        for task in new_tasks:
            task_title = task.get("title", "").strip().lower()
            priority = determine_task_priority(task.get("due_date"), task.get("priority"))
            try:
                page_id = await asyncio.to_thread(
                    add_task_to_notion,
                    title=task.get("title"),
                    source=task.get("source"),
                    due_date=task.get("due_date"),
                    url=task.get("url"),
                    course=task.get("course"),
                    task_type=task.get("task_type"),
                    priority=priority,
                    status="Not started",
                )
            except Exception as e:
                logger.error(f"Failed to auto-push task to Notion: {e}")
                continue

            if not page_id:
                logger.warning("Notion did not confirm task insertion: %s", task.get("title"))
                continue

            from bot.state import update_state
            update_state(lambda s, title=task_title: s.setdefault("seen_tasks", []).append(title))

            # ``True`` is the legacy signal from the Notion deduplicator that
            # an active row already exists. It is successful, but has no page
            # ID to attach interactive Telegram controls to.
            if not isinstance(page_id, str):
                continue
            successful_tasks.append((task, page_id, priority))

        if successful_tasks:
            tasks_str = "".join(
                f"{priority_emoji(priority)} {escape_html(task.get('title'))} — {escape_html(priority.capitalize())}\n"
                for task, _page_id, priority in successful_tasks
            )
            msg_text = (
                "<b>New tasks synced to Notion</b>\n\n"
                f"{tasks_str}\n"
                "Each task below has controls to adjust its priority or mark it started/done."
            )
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg_text,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=msg_text)

            # Keep the page IDs and display metadata locally so each Telegram
            # control can update the matching Notion row without another LLM
            # pass. Cap this small interaction cache to prevent state growth.
            pending_entries = []
            for index, (task, page_id, priority) in enumerate(successful_tasks, start=1):
                short_id = f"T{page_id.replace('-', '')[-6:].upper()}"
                pending_entries.append((short_id, task, page_id, priority, index))

            def _record_pending_tasks(current):
                pending_tasks = current.setdefault("pending_tasks", {})
                legacy_ids = current.setdefault("pending_priorities", {})
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                for short_id, task, page_id, priority, _index in pending_entries:
                    pending_tasks[short_id] = {
                        "page_id": page_id,
                        "title": task.get("title", "Untitled task"),
                        "source": task.get("source") or "Unknown source",
                        "course": task.get("course"),
                        "due_date": task.get("due_date"),
                        "priority": priority,
                        "status": "Not started",
                        "created_at": now,
                    }
                    legacy_ids[short_id] = page_id

                # Old task controls are no longer useful after a few months.
                if len(pending_tasks) > 100:
                    keep = sorted(
                        pending_tasks.items(),
                        key=lambda item: item[1].get("created_at", ""),
                        reverse=True,
                    )[:100]
                    current["pending_tasks"] = dict(keep)
                    current["pending_priorities"] = {
                        key: value["page_id"] for key, value in keep if value.get("page_id")
                    }

            update_state(_record_pending_tasks)

            for short_id, task, _page_id, priority, _index in pending_entries:
                source_line = task.get("source") or "Unknown source"
                if task.get("course"):
                    source_line += f" • {task['course']}"
                due_line = task.get("due_date") or "No due date"
                task_message = (
                    f"{priority_emoji(priority)} <b>{escape_html(task.get('title', 'Untitled task'))}</b>\n"
                    f"📌 {escape_html(source_line)}\n"
                    f"📅 Due: {escape_html(due_line)}\n"
                    f"Priority: <b>{escape_html(priority.capitalize())}</b>\n\n"
                    "Choose a priority or update its status:"
                )
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=task_message,
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_task_actions_keyboard(short_id),
                    )
                except Exception:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=task_message.replace("<b>", "").replace("</b>", ""),
                        reply_markup=get_task_actions_keyboard(short_id),
                    )

            history_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                f"chat_history_{chat_id}_notion.txt",
            )
            with open(history_file, "a") as f:
                f.write(f"System Background Job: {msg_text}\n")

    # 2. Telegram Digest
    digest = ai_result.get("digest", "")
    quiet_digest = digest.strip().startswith("✅ All caught up")
    if digest and digest != "Nothing to report right now!" and not quiet_digest:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_digest.txt"), "w") as f:
                f.write(digest)
        except Exception:
            pass
        await send_assistant_response(context, chat_id, digest, title="Briefing")

    # 3. Ask to Compile Mega Study Guides (with inline keyboard)
    topics = ai_result.get("topics", [])
    if topics:
        topics_str = "\n".join([f"- {t}" for t in topics])
        msg = (
            "I detected upcoming assignments or tests for these topics:\n"
            f"{topics_str}\n\n"
            "Choose a topic below to compile a study guide."
        )
        keyboard = get_digest_topic_keyboard(topics)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=render_assistant_text(msg, title="Study opportunities"),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=msg)

    # 4. Track correlations across sources
    try:
        correlate_items([
            {"source": "canvas", "title": t, "type": "assignment"}
            for t in ai_result.get("topics", [])
        ] + [
            {"source": "gmail", "title": e.get("title", ""), "type": "email"}
            for e in ai_result.get("tasks", [])
        ])
    except Exception as e:
        logger.warning(f"Correlation tracking failed: {e}")

    from bot.state import update_state
    update_state(lambda s: s.update({"seen_tasks": s.get("seen_tasks", [])[-config.MAX_SEEN_TASKS:]}))
    logger.info("Background job: Complete.")


# ── Telegram handlers ──────────────────────────────────────────────────────────

@require_auth
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # The service normally schedules this on boot.  For a direct/dev start,
    # create it only when it does not already exist; never duplicate jobs.
    current_jobs = list(context.job_queue.get_jobs_by_name(str(config.SANEL_CHAT_ID)))
    if not current_jobs:
        context.job_queue.run_repeating(
            check_updates,
            interval=config.DIGEST_INTERVAL_SECONDS,
            first=5,
            chat_id=config.SANEL_CHAT_ID,
            name=str(config.SANEL_CHAT_ID),
        )

    text, keyboard = dashboard_message()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ── Concurrency Locks ─────────────────────────────────────────────────────────
from bot.state import is_sleep_window, get_user_lock
digest_lock = asyncio.Lock()  # prevents overlapping check_updates
watchdog_lock = asyncio.Lock()  # prevents overlapping watchdog


# ── Telegram handlers ──────────────────────────────────────────────────────────

@require_auth
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_sleep_window():
        await update.message.reply_text("💤 I am currently in Sleep Mode optimizing my brain. I will be back online at 7 AM ET!")
        return

    user_text = update.message.text
    chat_id   = update.effective_chat.id

    if user_text.strip().lower() in ("help", "commands", "what can you do", "cmds", "/help", "/"):
        await help_command(update, context)
        return

    if user_text.strip().lower() == "models":
        context.args = []
        await model_command(update, context)
        return

    if update.message.reply_to_message and update.message.reply_to_message.text:
        reply_text = update.message.reply_to_message.text
        user_text = f"[In reply to your message: \"{reply_text}\"]\n\n{user_text}"

    # Send a "thinking" indicator
    thinking_msg = await begin_progress(
        context,
        chat_id,
        "I’m working on your request. If another request is running, yours will continue as soon as it is ready.",
    )

    try:
        user_lock = get_user_lock(chat_id)
        async with user_lock:
            reply = await send_to_antigravity_and_wait(user_text, chat_id, context, thinking_msg)

        log_event("message", {"preview": user_text[:50], "routed_to": "unknown"}, notify=False)

        await edit_progress(context, chat_id, thinking_msg.message_id, "Your response is ready below.")
        await send_assistant_response(context, chat_id, reply)
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        log_event("error", {"message": str(e)[:80], "source": "handle_message"})
        try:
            await edit_progress(
                context,
                chat_id,
                thinking_msg.message_id,
                "**I couldn’t complete that request.**\n\nPlease try again in a moment. If it persists, open More → Diagnostics for the next step.",
            )
        except Exception:
            pass



@require_auth
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download voice message, transcribe locally, route through AI."""
    chat_id = update.effective_chat.id
    if chat_id != config.SANEL_CHAT_ID:
        await update.message.reply_text("")
        return

    msg = await begin_progress(context, chat_id, "Transcribing your voice note locally.")

    try:
        voice_file = await update.message.voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            await voice_file.download_to_drive(tmp_path)
            transcription = await asyncio.to_thread(transcribe_voice, tmp_path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        if transcription.startswith("❌"):
            await edit_progress(context, chat_id, msg.message_id, "**I couldn’t transcribe that voice note.**\n\nPlease try a clearer recording or send the request as text.")
            return

        await edit_progress(
            context,
            chat_id,
            msg.message_id,
            f"**Transcription complete**\n\n{transcription[:200]}{'...' if len(transcription) > 200 else ''}\n\nThinking…",
        )

        # Route transcription through the AI
        user_lock = get_user_lock(chat_id)
        async with user_lock:
            reply = await send_to_antigravity_and_wait(transcription, chat_id, context, msg)

        await edit_progress(context, chat_id, msg.message_id, "Your response is ready below.")
        await send_assistant_response(context, chat_id, reply)

    except Exception as e:
        logger.error(f"Error handling voice: {e}")
        try:
            await edit_progress(context, chat_id, msg.message_id, "**I couldn’t process that voice note.**\n\nPlease try again or send the request as text.")
        except Exception:
            pass


@require_auth
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Downloads a photo sent to the bot, saves it, and asks the AI to process it."""
    chat_id = update.effective_chat.id

    msg = await begin_progress(context, chat_id, "Downloading your image for local processing.", action="upload_photo")

    # Get the largest resolution photo
    photo_file = await update.message.photo[-1].get_file()
    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
        download_path = tmp.name

    try:
        await photo_file.download_to_drive(download_path)

        caption = update.message.caption or ""
        await edit_progress(context, chat_id, msg.message_id, "Reading the text in your image locally.")

        try:
            import pytesseract
            from PIL import Image
            def _ocr() -> str:
                with Image.open(download_path) as image:
                    return pytesseract.image_to_string(image)
            ocr_text = await asyncio.to_thread(_ocr)
            if not ocr_text.strip():
                ocr_text = "(No text found in image)"
            log_event("photo", {"ocr_chars": len(ocr_text), "has_question": bool(caption.strip())}, notify=False)
        except Exception as e:
            log_event("error", {"message": str(e)[:80], "source": "ocr"})
            await edit_progress(context, chat_id, msg.message_id, "**I couldn’t read that image.**\n\nTry a clearer photo with more contrast, then send it again.")
            return
    finally:
        if os.path.exists(download_path):
            os.unlink(download_path)

    # If the user asked a question in the caption, route the OCR text into the primary AI so it can use the PDFs
    if caption.strip():
        await edit_progress(context, chat_id, msg.message_id, "Analyzing your question with the knowledge base and your uploaded text.")
        user_text = f"[I have uploaded a photo. Here is the exact text written in the photo:\n{ocr_text}]\n\nMy Question: {caption}"
        try:
            user_lock = get_user_lock(chat_id)
            async with user_lock:
                reply = await send_to_antigravity_and_wait(user_text, chat_id, context, msg)

            await edit_progress(context, chat_id, msg.message_id, "Your photo answer is ready below.")
            await send_assistant_response(context, chat_id, reply)
            return
        except Exception as e:
            logger.error(f"Error answering photo question: {e}")
            await edit_progress(context, chat_id, msg.message_id, "**I couldn’t analyze that photo.**\n\nTry again with a clearer image or ask your question as text.")
            return

    await edit_progress(context, chat_id, msg.message_id, "Checking the image for assignments and deadlines.")

    prompt = (
        "You are an offline filtering AI. Read the text extracted from this photo.\n"
        "Your job is to extract homework assignments, projects, or mandatory deadlines.\n"
        "If you see lists of numbers (e.g., 'Drills: 456, 460'), dates, or the word 'homework'/'due', you MUST extract them or reply 'UNSURE'.\n"
        "Only if you are 100% certain there is no actionable task, reply exactly with: 'NO_ALERT'\n"
        "CRITICAL RULE: If the text is messy and you cannot confidently parse it, reply exactly with: 'UNSURE'\n\n"
        f"Caption: {caption}\nPhoto OCR Text:\n{ocr_text}"
    )

    extracted = "UNSURE"
    from llm_router import call_local_rpc
    try:
        # Try local RPC first (no rate limits).
        # Offload the blocking (sync httpx, up to 120s) call to a worker thread
        # so it doesn't stall the asyncio event loop and freeze the whole bot.
        extracted = await asyncio.to_thread(
            call_local_rpc,
            prompt=prompt,
            max_tokens=200,
            timeout=120,
            allow_cloud=False,
        )
        if extracted and "⚠️ Local inference unavailable" in extracted:
            extracted = "UNSURE"
    except Exception:
        logger.info("Local photo extraction unavailable; preserving OCR for the next local digest")
        extracted = "UNSURE"

    if extracted:
        if "NO_ALERT" not in extracted.upper() and "UNSURE" not in extracted.upper():
            config.IMPORTANT_EXTRACTS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(config.IMPORTANT_EXTRACTS_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n--- Photo Upload ---\n{extracted}\n")
            reply = f"✅ Important text found and saved for the next digest!\n\n_Filtered preview:_\n{extracted}"
        else:
            # User specifically sent a photo, so it's important regardless of what the small model thinks.
            config.IMPORTANT_EXTRACTS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(config.IMPORTANT_EXTRACTS_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n--- Photo Upload (Raw OCR) ---\n{ocr_text}\n")
            reply = "⚠️ I couldn’t identify a specific assignment locally, but saved the extracted text for your next local digest."
    else:
        reply = "⚠️ I couldn’t identify an assignment from that image. The text was not sent to a cloud provider."

    try:
        await edit_progress(
            context,
            chat_id,
            msg.message_id,
            reply,
            reply_markup=get_photo_response_keyboard(),
        )
    except Exception:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=reply, reply_markup=get_photo_response_keyboard())

async def nightly_wrapper(context: ContextTypes.DEFAULT_TYPE):
    """Run bounded, local-only maintenance and report the real outcome."""
    chat_id = context.job.chat_id
    from scrapers.memory_consolidation import consolidate_memory
    from scrapers.nightly_processor import run_nightly_job
    from scrapers.web_precacher import pre_cache_web

    progress = await begin_progress(context, chat_id, "Starting the nightly local maintenance cycle.")
    stages: list[str] = []
    failures: list[str] = []
    try:
        try:
            from scrapers.canvas_scraper import queue_recent_canvas_study_files
            discovered = await asyncio.to_thread(queue_recent_canvas_study_files)
            stages.append(f"queued {discovered} Canvas file(s)")
        except Exception as exc:
            logger.warning("Nightly source discovery failed: %s", type(exc).__name__)
            failures.append("source discovery")

        await edit_progress(context, chat_id, progress.message_id, "Processing queued study documents locally.")
        documents = await run_nightly_job(context.bot, chat_id)
        stages.append(documents.detail or documents.status.value)
        if not documents.ok:
            failures.append("document processing")

        await edit_progress(context, chat_id, progress.message_id, "Consolidating private memory and refreshing retrieval.")
        memory = await consolidate_memory()
        stages.append(memory.detail or memory.status.value)
        if not memory.ok:
            failures.append("memory consolidation")

        # Web enrichment is off by default and has its own outbound policy.
        await pre_cache_web()
        stages.append("web enrichment checked")
    except Exception as exc:
        logger.exception("Nightly cycle failed")
        failures.append("unexpected maintenance error")

    if failures:
        log_nightly("maintenance", "partial", {"failed_stages": failures, "stages": stages})
        await edit_progress(
            context,
            chat_id,
            progress.message_id,
            "Nightly maintenance finished with recoverable issues: " + ", ".join(failures) + ". Queued work was retained for retry.",
        )
    else:
        log_nightly("maintenance", "completed", {"stages": stages})
        await edit_progress(context, chat_id, progress.message_id, "Nightly local maintenance completed: " + "; ".join(stages) + ".")

# ── NEW COMMAND HANDLERS ──────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        config.initialize_runtime()
        config.validate_runtime_config("bot")
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    configure_logging()

    async def post_shutdown(app):
        try:
            from bot.runtime import cleanup_background_tasks
            await cleanup_background_tasks()
        finally:
            from utils import cleanup_async_caches
            from llm_router import cleanup_llm_clients
            await cleanup_async_caches()
            await cleanup_llm_clients()
            close_activity_log()

    async def post_init(app):
        """Expose only the everyday power-user commands in Telegram's menu."""
        try:
            await app.bot.set_my_commands([
                BotCommand("start", "Open the assistant dashboard"),
                BotCommand("help", "Browse help by category"),
                BotCommand("summary", "Refresh today's digest"),
                BotCommand("canvas", "Check current coursework"),
                BotCommand("calendar", "Manage assignment calendar"),
                BotCommand("model", "View or choose an AI model"),
                BotCommand("ping", "Check assistant health"),
            ])
        except Exception:
            logger.warning("Unable to update Telegram command menu", exc_info=True)

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        error = context.error
        logger.exception("Unhandled Telegram update failure", exc_info=error)
        log_event("error", {"source": "telegram_update", "error_type": type(error).__name__})
        # The user-facing boundary should never expose exception details or
        # attempt delivery to an untrusted chat.
        effective_chat = getattr(update, "effective_chat", None)
        effective_user = getattr(update, "effective_user", None)
        if (
            getattr(effective_chat, "id", None) == config.TELEGRAM_CHAT_ID
            and getattr(effective_user, "id", None) == config.TELEGRAM_OWNER_USER_ID
            and getattr(effective_chat, "type", None) == "private"
        ):
            try:
                await context.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text="I hit an unexpected error. No action was completed; please retry once.",
                )
            except Exception:
                logger.warning("Unable to send generic update error", exc_info=True)

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_error_handler(error_handler)

    # Auto-start the background 4-hour digest task for the user on boot
    SANEL_CHAT_ID = config.SANEL_CHAT_ID
    job_queue = app.job_queue

    # Enforce rotations and compile context before first run
    try:
        enforce_all_rotations()
    except Exception as e:
        logger.warning(f"Initial rotation enforcement failed: {e}")

    try:
        from scrapers.compile_context import compile_bot_context
        asyncio.run(compile_bot_context())
    except Exception as e:
        logger.error(f"Failed to pre-compile bot context: {e}")

    try:
        from scrapers.assignment_calendar import AssignmentCalendarService
        calendar_service = AssignmentCalendarService()
        if calendar_service.store.is_enabled():
            calendar_service.sync_official()
            logger.info("Initial assignment calendar sync completed.")
    except Exception as e:
        logger.warning(f"Initial calendar sync failed: {e}")

    import time as _time
    try:
        last_mtime = os.path.getmtime(config.LATEST_DIGEST_FILE)
        elapsed = _time.time() - last_mtime
        time_until_next = max(5, int(config.DIGEST_INTERVAL_SECONDS - elapsed))
    except Exception:
        time_until_next = 5

    job_queue.run_repeating(check_updates, interval=config.DIGEST_INTERVAL_SECONDS, first=time_until_next, chat_id=SANEL_CHAT_ID, name=str(SANEL_CHAT_ID))

    async def run_rotation_enforcement(context: ContextTypes.DEFAULT_TYPE):
        await asyncio.to_thread(enforce_all_rotations)

    async def run_daily_backup(context: ContextTypes.DEFAULT_TYPE):
        await asyncio.to_thread(create_backup)

    # Run rotation enforcement every 6 hours
    job_queue.run_repeating(run_rotation_enforcement, interval=21600, first=21600, chat_id=SANEL_CHAT_ID, name="rotation_enforcement")

    # Daily backup at 3 AM ET
    job_queue.run_daily(
        run_daily_backup,
        time=datetime.time(hour=3, minute=0, tzinfo=ZoneInfo('America/New_York')),
        chat_id=SANEL_CHAT_ID, name="daily_backup"
    )

    async def morning_wrapper(context: ContextTypes.DEFAULT_TYPE):
        try:
            from scrapers.morning_digest import send_morning_digest
            await send_morning_digest()
        except Exception as e:
            logger.error(f"Morning digest error: {e}")

    # Auto-start the 30-minute watchdog
    job_queue.run_repeating(watchdog_check, interval=config.WATCHDOG_INTERVAL_SECONDS, first=1800, chat_id=SANEL_CHAT_ID, name=f"{SANEL_CHAT_ID}_watchdog")

    # Run the offline Llama PDF processor every night at 2:00 AM ET
    job_queue.run_daily(nightly_wrapper, time=datetime.time(hour=1, minute=0, tzinfo=ZoneInfo('America/New_York')), chat_id=SANEL_CHAT_ID, name=f"{SANEL_CHAT_ID}_nightly")

    # Run the Morning Digest every morning at 7:00 AM ET
    job_queue.run_daily(morning_wrapper, time=datetime.time(hour=7, minute=0, tzinfo=ZoneInfo('America/New_York')), chat_id=SANEL_CHAT_ID, name=f"{SANEL_CHAT_ID}_morning")

    from telegram.ext import CallbackQueryHandler
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(CommandHandler("model", model_command))
    app.add_handler(CommandHandler("summary", summary_command))
    app.add_handler(CommandHandler("bash", bash_command))
    app.add_handler(CommandHandler("p", priority_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(CommandHandler("correlations", correlations_command))
    app.add_handler(CommandHandler("classroom", classroom_pdfs_command))
    app.add_handler(CommandHandler("canvas", canvas_command))
    app.add_handler(CommandHandler("calendar", calendar_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("server", server_command))
    app.add_handler(CommandHandler("errors", errors_command))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))



    print("🤖 Antigravity Telegram bridge is running...")

    # Run with graceful shutdown
    try:
        app.run_polling(drop_pending_updates=False)
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Bot stopped. Rotating files before exit...")
        enforce_all_rotations()
