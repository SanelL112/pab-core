import os
import sys
import json
import asyncio
import subprocess
import logging
import secrets
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from bot.security import require_auth
from bot.state import load_state, update_state
from bot.ui import (
    HELP_TEXT,
    SECTION_TEXT,
    confirmation_keyboard,
    escape_html,
    help_keyboard,
    home_keyboard,
    home_text,
    navigation_keyboard,
    render_assistant_text,
    section_keyboard,
    send_assistant_response,
)
from utils import create_backup, get_correlation_summary, get_health_status, list_backups, restore_backup, run_bash_safely
from config import SANEL_CHAT_ID, GROUPME_GROUP_ID, LATEST_DIGEST_FILE, MAX_SEEN_TASKS
from bot.runtime import _track_task
from scrapers.mega_study_builder import build_guide_for_drive_file
from llm_router import get_cost_summary
import time

logger = logging.getLogger(__name__)

_CONFIRMATION_TTL_SECONDS = 120


def _confirmation_store(context) -> dict[str, dict]:
    """Return the short-lived, per-user confirmation registry."""
    store = context.user_data.setdefault("_confirmations", {})
    now = time.monotonic()
    for nonce, item in list(store.items()):
        if not isinstance(item, dict) or item.get("expires_at", 0) <= now:
            store.pop(nonce, None)
    return store


def _issue_confirmation(context, action: str, *, cancel: str = "nav:more") -> InlineKeyboardMarkup:
    """Bind one destructive callback to a nonce that expires after two minutes."""
    nonce = secrets.token_urlsafe(9)
    _confirmation_store(context)[nonce] = {
        "action": action,
        "expires_at": time.monotonic() + _CONFIRMATION_TTL_SECONDS,
    }
    return confirmation_keyboard(f"confirm:{nonce}", cancel=cancel)


def _consume_confirmation(context, callback_data: str) -> str | None:
    """Consume a one-time callback and return its authorized action."""
    if not callback_data.startswith("confirm:"):
        return None
    nonce = callback_data.removeprefix("confirm:")
    item = _confirmation_store(context).pop(nonce, None)
    if not isinstance(item, dict) or item.get("expires_at", 0) <= time.monotonic():
        return None
    action = item.get("action")
    return action if isinstance(action, str) else None


def _pending_task(state: dict, short_id: str) -> dict | None:
    """Resolve both the new rich task state and legacy priority-only IDs."""
    task = state.get("pending_tasks", {}).get(short_id)
    if isinstance(task, dict) and task.get("page_id"):
        return task
    page_id = state.get("pending_priorities", {}).get(short_id)
    return {"page_id": page_id, "title": short_id} if page_id else None


def _update_pending_task(short_id: str, **changes) -> None:
    def mutate(state):
        task = state.setdefault("pending_tasks", {}).get(short_id)
        if isinstance(task, dict):
            task.update(changes)
        # Keep the backwards-compatible mapping alive for /p users.
        if task and task.get("page_id"):
            state.setdefault("pending_priorities", {})[short_id] = task["page_id"]

    update_state(mutate)


def _render_task_control(task: dict, confirmation: str | None = None) -> str:
    """Render the current state after a Telegram task button is pressed."""
    from scrapers.notion_client import priority_emoji

    priority = str(task.get("priority", "medium"))
    source = str(task.get("source") or "Unknown source")
    if task.get("course"):
        source += f" • {task['course']}"
    text = (
        f"{priority_emoji(priority)} <b>{escape_html(task.get('title', 'Untitled task'))}</b>\n"
        f"📌 {escape_html(source)}\n"
        f"📅 Due: {escape_html(task.get('due_date') or 'No due date')}\n"
        f"Priority: <b>{escape_html(priority.capitalize())}</b>\n"
        f"Status: <b>{escape_html(task.get('status', 'Not started'))}</b>"
    )
    return text + (f"\n\n{confirmation}" if confirmation else "\n\nChoose a priority or update its status:")


def dashboard_message() -> tuple[str, object]:
    """Return the home screen from current durable task state."""
    pending = load_state().get("pending_tasks", {})
    active_count = sum(
        1 for task in pending.values()
        if isinstance(task, dict) and task.get("status") != "Done"
    )
    return home_text(active_count), home_keyboard()


async def _edit_ui(query, text: str, *, reply_markup=None) -> None:
    """Edit an inline UI screen using the shared HTML presentation contract."""
    await query.edit_message_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


def _error_screen(title: str, guidance: str) -> str:
    return (
        f"<b>{escape_html(title)}</b>\n\n"
        f"{escape_html(guidance)}\n\n"
        "No changes were made. You can retry or return Home."
    )


async def _handle_ui_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle dashboard and menu callbacks; return False for legacy callbacks."""
    query = update.callback_query
    original_data = query.data or ""
    confirmed_action = _consume_confirmation(context, original_data)
    if original_data.startswith("confirm:") and confirmed_action is None:
        await _edit_ui(
            query,
            _error_screen("That confirmation expired", "Open the action again to create a new confirmation."),
            reply_markup=home_keyboard(),
        )
        return True
    data = confirmed_action or original_data

    if data == "nav:home":
        text, keyboard = dashboard_message()
        await _edit_ui(query, text, reply_markup=keyboard)
        return True

    section = data.removeprefix("nav:") if data.startswith("nav:") else ""
    if section in SECTION_TEXT:
        await _edit_ui(query, SECTION_TEXT[section], reply_markup=section_keyboard(section))
        return True
    if data == "nav:help":
        await _edit_ui(
            query,
            "<b>Help</b>\n\nChoose a category for a short, task-focused guide. "
            "All slash commands remain available as power-user shortcuts.",
            reply_markup=help_keyboard(),
        )
        return True
    if data.startswith("help:"):
        category = data.split(":", 1)[1]
        if category in HELP_TEXT:
            await _edit_ui(
                query,
                HELP_TEXT[category],
                reply_markup=InlineKeyboardMarkup(navigation_keyboard(back="nav:help")),
            )
        else:
            await _edit_ui(query, _error_screen("That help page is unavailable", "Open Help again and choose a category."), reply_markup=home_keyboard())
        return True

    if data == "ask:voice":
        await _edit_ui(
            query,
            "<b>Voice note</b>\n\nRecord and send a voice note in this chat. I’ll transcribe it locally, then answer it using the same privacy routing as typed messages.",
            reply_markup=InlineKeyboardMarkup(navigation_keyboard(back="nav:ask")),
        )
        return True
    if data == "ask:photo":
        await _edit_ui(
            query,
            "<b>Photo help</b>\n\nSend a photo with your question in its caption. I’ll extract the text and use it to answer the question. For practice grading, include the subject or topic.",
            reply_markup=InlineKeyboardMarkup(navigation_keyboard(back="nav:ask")),
        )
        return True

    if data in {"study:notion", "nav:tasks", "nav:notion"}:
        await _edit_ui(query, "<b>Loading Notion tasks…</b>\n\nRetrieving your active tasks from Notion.")
        try:
            from scrapers.notion_client import get_notion_tasks_summary
            summary = await asyncio.to_thread(get_notion_tasks_summary, 20)
            await _edit_ui(query, render_assistant_text(summary, title="Notion Tasks"), reply_markup=section_keyboard("study"))
        except Exception:
            logger.exception("Notion tasks fetch failed")
            await _edit_ui(query, _error_screen("Notion is unavailable", "Check your Notion API connection and retry."), reply_markup=section_keyboard("study"))
        return True

    if data == "study:assignments":
        await _edit_ui(query, "<b>Checking assignments…</b>\n\nI’m retrieving the current Canvas coursework.")
        try:
            from scrapers.canvas_scraper import get_upcoming_assignments
            result = await asyncio.to_thread(get_upcoming_assignments)
            await _edit_ui(query, render_assistant_text(result, title="Assignments"), reply_markup=section_keyboard("study"))
        except Exception:
            logger.exception("Study assignment check failed")
            await _edit_ui(query, _error_screen("I couldn’t check assignments", "Try again in a moment."), reply_markup=section_keyboard("study"))
        return True
    if data == "study:grade":
        await _edit_ui(
            query,
            "<b>Grade a practice photo</b>\n\nSend a clear photo of your completed work with a caption such as <code>SAT Math — check questions 1–10</code>.",
            reply_markup=InlineKeyboardMarkup(navigation_keyboard(back="nav:study")),
        )
        return True
    if data == "study:guide":
        await _edit_ui(
            query,
            "<b>Build a study guide</b>\n\nSend the topic you want to study, for example <code>Build a guide for circle geometry</code>. If a digest detects topics, it also offers one-tap guide buttons.",
            reply_markup=InlineKeyboardMarkup(navigation_keyboard(back="nav:study")),
        )
        return True

    if data == "cal:status":
        try:
            from scrapers.assignment_calendar import AssignmentCalendarService
            status = await asyncio.to_thread(AssignmentCalendarService().status)
            text = (
                "<b>Calendar status</b>\n\n"
                f"Sync: <b>{'Enabled' if status['enabled'] else 'Disabled'}</b>\n"
                f"Local CalDAV: {'Ready' if status['caldav_configured'] else 'Not configured'}\n"
                f"Google mirror: {'Ready' if status['google_configured'] else 'Not configured'}\n"
                f"Tracked events: <b>{status['tracked_events']}</b>"
            )
            await _edit_ui(query, text, reply_markup=section_keyboard("calendar"))
        except Exception:
            logger.exception("Calendar status failed")
            await _edit_ui(query, _error_screen("Calendar status is unavailable", "Check your calendar configuration and try again."), reply_markup=section_keyboard("calendar"))
        return True
    if data == "cal:preview":
        try:
            from scrapers.assignment_calendar import AssignmentCalendarService, format_preview
            from inline_keyboards import get_calendar_proposal_keyboard
            await _edit_ui(query, "<b>Preparing calendar preview…</b>\n\nNo calendar changes are being made.")
            actions, batch_id, recommended = await asyncio.to_thread(AssignmentCalendarService().preview)
            keyboard_rows = []
            if batch_id:
                keyboard_rows.extend(get_calendar_proposal_keyboard(batch_id).inline_keyboard)
            keyboard_rows.extend(navigation_keyboard(back="nav:calendar"))
            await _edit_ui(
                query,
                render_assistant_text(format_preview(actions, batch_id, recommended), title="Calendar preview"),
                reply_markup=InlineKeyboardMarkup(keyboard_rows),
            )
        except Exception:
            logger.exception("Calendar preview failed")
            await _edit_ui(query, _error_screen("I couldn’t prepare a calendar preview", "Check that your connected sources are available, then retry."), reply_markup=section_keyboard("calendar"))
        return True
    if data in {"cal:enable:confirm", "cal:disable:confirm", "cal:sync:confirm"}:
        label = {
            "cal:enable:confirm": "enable assignment-calendar sync",
            "cal:disable:confirm": "disable future assignment-calendar sync",
            "cal:sync:confirm": "sync official assignment changes now",
        }[data]
        action = data.removesuffix(":confirm")
        await _edit_ui(
            query,
            f"<b>Confirm action</b>\n\nDo you want to {label}? Existing calendar events will not be deleted.",
            reply_markup=_issue_confirmation(context, action, cancel="nav:calendar"),
        )
        return True
    if data in {"cal:enable", "cal:disable", "cal:sync"}:
        if confirmed_action != data:
            # Legacy persistent buttons are deliberately upgraded to an
            # expiring confirmation rather than remaining replayable writes.
            await _edit_ui(
                query,
                "<b>Confirm action</b>\n\nThis action needs a fresh confirmation.",
                reply_markup=_issue_confirmation(context, data, cancel="nav:calendar"),
            )
            return True
        try:
            from scrapers.assignment_calendar import AssignmentCalendarService
            service = AssignmentCalendarService()
            if data == "cal:enable":
                await asyncio.to_thread(service.enable)
                text = "<b>Calendar sync enabled</b>\n\nOfficial Canvas and Classroom work will sync on the next source refresh."
            elif data == "cal:disable":
                await asyncio.to_thread(service.disable)
                text = "<b>Calendar sync disabled</b>\n\nExisting events were left unchanged."
            else:
                await _edit_ui(query, "<b>Syncing calendar…</b>\n\nChecking official assignments now.")
                applied = await asyncio.to_thread(service.sync_official)
                text = f"<b>Calendar synced</b>\n\nApplied <b>{applied}</b> official assignment change(s)."
            await _edit_ui(query, text, reply_markup=section_keyboard("calendar"))
        except Exception:
            logger.exception("Calendar action failed")
            await _edit_ui(query, _error_screen("Calendar action failed", "Review the calendar status, then try again."), reply_markup=section_keyboard("calendar"))
        return True

    if data == "more:health":
        await _edit_ui(query, render_assistant_text(get_health_status(), title="Health"), reply_markup=section_keyboard("more"))
        return True
    if data == "more:stats":
        await _edit_ui(query, render_assistant_text(get_cost_summary(), title="Usage"), reply_markup=section_keyboard("more"))
        return True
    if data == "more:backup:confirm":
        await _edit_ui(query, "<b>Create a backup?</b>\n\nThis creates a new backup of the bot’s critical state files.", reply_markup=_issue_confirmation(context, "more:backup"))
        return True
    if data == "more:backup":
        if confirmed_action != data:
            await _edit_ui(
                query,
                "<b>Create a backup?</b>\n\nThis action needs a fresh confirmation.",
                reply_markup=_issue_confirmation(context, "more:backup"),
            )
            return True
        try:
            await _edit_ui(query, "<b>Creating backup…</b>\n\nPlease keep this chat open for a moment.")
            path = await asyncio.to_thread(create_backup)
            text = f"<b>Backup created</b>\n\n<code>{escape_html(os.path.basename(path))}</code>" if path else _error_screen("Backup failed", "Try again after checking available storage.")
            await _edit_ui(query, text, reply_markup=section_keyboard("more"))
        except Exception:
            logger.exception("Backup failed")
            await _edit_ui(query, _error_screen("Backup failed", "Try again after checking available storage."), reply_markup=section_keyboard("more"))
        return True
    if data == "more:models":
        await _edit_ui(query, "<b>Models</b>\n\nYour current routing protects sensitive content automatically. Use <code>/model</code> to view or choose a specific model.", reply_markup=section_keyboard("more"))
        return True
    if data == "more:server":
        await _edit_ui(query, "<b>Server tools</b>\n\nUse <code>/server</code> to open the available server modules. Start/stop controls and shell access remain command-only for safety.", reply_markup=section_keyboard("more"))
        return True
    if data == "more:diagnostics":
        await _edit_ui(query, "<b>Diagnostics</b>\n\nUse <code>/errors</code> for a recent log summary or <code>/correlations</code> for cross-source data insights. Detailed provider errors stay in logs rather than normal conversations.", reply_markup=section_keyboard("more"))
        return True

    return False

@require_auth
async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch or display the active assistant study persona."""
    chat_id = update.effective_chat.id
    args = context.args
    state = load_state()
    current_mode = state.get("user_modes", {}).get(str(chat_id), "default")

    if not args:
        from bot.ui import mode_keyboard
        mode_names = {
            "default": "🤖 Default Assistant",
            "tutor": "🎓 Socratic Tutor",
            "quick": "⚡ Quick / Concise",
            "drill": "🎯 Exam Drill",
        }
        await update.message.reply_text(
            f"Current chat mode: <b>{mode_names.get(current_mode, current_mode)}</b>\n\n"
            "Select a study personality below:",
            parse_mode=ParseMode.HTML,
            reply_markup=mode_keyboard(current_mode),
        )
        return

    requested = args[0].lower()
    valid_modes = {"default", "tutor", "quick", "drill"}
    if requested not in valid_modes:
        await update.message.reply_text("❌ Invalid mode. Choose from: <code>default</code>, <code>tutor</code>, <code>quick</code>, <code>drill</code>", parse_mode=ParseMode.HTML)
        return

    update_state(lambda s: s.setdefault("user_modes", {}).update({str(chat_id): requested}))
    await update.message.reply_text(f"✅ Chat mode switched to <b>{requested.capitalize()}</b>.", parse_mode=ParseMode.HTML)


@require_auth
async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear the active multi-turn chat session memory."""
    chat_id = update.effective_chat.id
    from bot.ai_bridge import clear_session
    clear_session(chat_id)
    await update.message.reply_text("🧹 Conversation context cleared. Starting a fresh session!", parse_mode=ParseMode.HTML)


@require_auth
async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    args = context.args
    
    FREE_ALIASES = {
        "llama3.3": "openrouter:meta-llama/llama-3.3-70b-instruct:free",
        "llama3.2": "openrouter:meta-llama/llama-3.2-3b-instruct:free",
        "hermes": "openrouter:nousresearch/hermes-3-llama-3.1-405b:free",
        "ultra": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
        "nemotron-super": "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
        "nemotron-safety": "openrouter:nvidia/nemotron-3.5-content-safety:free",
        "nemotron-nano": "openrouter:nvidia/nemotron-3-nano-30b-a3b:free",
        "nemotron-omni": "openrouter:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "nemotron-vl": "openrouter:nvidia/nemotron-nano-12b-v2-vl:free",
        "nemotron-9b": "openrouter:nvidia/nemotron-nano-9b-v2:free",
        "nex": "openrouter:nex-agi/nex-n2-pro:free",
        "laguna": "openrouter:poolside/laguna-m.1:free",
        "laguna-xs": "openrouter:poolside/laguna-xs.2:free",
        "gpt-oss": "openrouter:openai/gpt-oss-120b:free",
        "gpt-oss-20b": "openrouter:openai/gpt-oss-20b:free",
        "gemma": "openrouter:google/gemma-4-31b-it:free",
        "gemma-26b": "openrouter:google/gemma-4-26b-a4b-it:free",
        "cohere": "openrouter:cohere/north-mini-code:free",
        "qwen-next": "openrouter:qwen/qwen3-next-80b-a3b-instruct:free",
        "qwen-coder": "openrouter:qwen/qwen3-coder:free",
        "lyria": "openrouter:google/lyria-3-pro-preview",
        "lyria-clip": "openrouter:google/lyria-3-clip-preview",
        "liquid": "openrouter:liquid/lfm-2.5-1.2b-thinking:free",
        "liquid-instruct": "openrouter:liquid/lfm-2.5-1.2b-instruct:free",
        "dolphin": "openrouter:cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
        "free": "openrouter:openrouter/free"
    }
    valid_local = ["auto", "flash", "pro", "local", "gemini", "agy", "agy:flash", "agy:pro"]
    
    if not args:
        state = load_state()
        current = state.get("user_models", {}).get(str(chat_id), "auto")
        display_current = current.replace("openrouter:", "") if current.startswith("openrouter:") else current
        from bot.ui import model_selection_keyboard
        await update.message.reply_text(
            f"Active inference engine: <b>{escape_html(display_current)}</b>\n\n"
            "Select an engine below or specify with <code>/model &lt;name&gt;</code>:\n"
            "• <b>Auto</b>: Google Gemini 3.7 Flash + Local Cluster\n"
            "• <b>Gemini Flash / Pro</b>: State-of-the-art Google models via AGY\n"
            "• <b>Local Cluster</b>: 100% On-Device private inference\n"
            "• <b>Free Cloud</b>: Llama 3.3 70B, Qwen Coder",
            parse_mode=ParseMode.HTML,
            reply_markup=model_selection_keyboard(current),
        )
        return
        
    requested = args[0].lower()
    
    # 1. Map alias to full OpenRouter model
    is_safe_alias = False
    if requested in FREE_ALIASES:
        requested = FREE_ALIASES[requested]
        is_safe_alias = True
        
    # 2. Check validity and ENFORCE safety for manual entries
    if requested.startswith("openrouter:"):
        if not is_safe_alias and not requested.endswith(":free"):
            requested += ":free" # Force the free endpoint so it never costs money
    elif requested not in valid_local:
        await update.message.reply_text("❌ Invalid model choice. Type `/model` to see available options.")
        return
        
    update_state(
        lambda state: state.setdefault("user_models", {}).update({str(chat_id): requested})
    )
    
    display_name = requested.replace("openrouter:", "") if requested.startswith("openrouter:") else requested
    await update.message.reply_text(f"Model safely switched to *{display_name}* ✅", parse_mode="Markdown")


@require_auth
async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    from bot.ui import begin_progress, edit_progress
    msg = await begin_progress(context, chat_id, "Gathering your latest school and communication updates.")
    
    import sys
    # sys.path already set at module level
    from scrapers.canvas_scraper import get_all_canvas_data
    from scrapers.groupme_scraper import get_latest_messages
    from scrapers.google_scraper import get_unread_emails, get_classroom_assignments, get_classroom_announcements, get_recent_google_docs
    from ai_processor import process_all_sources
    from scrapers.notion_client import add_task_to_notion
    
    # Move ALL data gathering into asyncio.to_thread to avoid blocking the event loop
    def gather_all_data():
        canvas = get_all_canvas_data() or "No Canvas"
        classroom = get_classroom_assignments() or "No Classroom"
        gmail = get_unread_emails() or "No Gmail"
        groupme = get_latest_messages(GROUPME_GROUP_ID) or "No GroupMe"
        announcements = get_classroom_announcements() or "No Announcements"
        docs = get_recent_google_docs() or "No Docs"
        try:
            from scrapers.notion_client import get_notion_tasks_summary
            notion_summary = get_notion_tasks_summary() or "No pending Notion tasks."
        except Exception:
            notion_summary = "No pending Notion tasks."
        return canvas, classroom, gmail, groupme, announcements, docs, notion_summary
    
    try:
        canvas, classroom, gmail, groupme, announcements, docs, notion_summary = await asyncio.to_thread(gather_all_data)
        ai_result = await asyncio.to_thread(process_all_sources, canvas, classroom, gmail, groupme, announcements, docs, notion_summary)
    except Exception as e:
        logger.error(f"Error during AI digest generation: {e}")
        await edit_progress(
            context,
            chat_id,
            msg.message_id,
            "**I couldn’t refresh your digest.**\n\nPlease try again in a moment. Your existing tasks and calendar were not changed.",
        )
        return
        
    # Ask user before pushing tasks to Notion
    import difflib
    new_tasks = []

    def record_new_tasks(state):
        """Deduplicate and persist the manual digest in one state transaction."""
        seen_titles = state.setdefault("seen_tasks", [])
        for task in ai_result.get("tasks", []):
            task_title = task.get("title", "").strip().lower()
            if not task_title:
                continue

            if any(
                difflib.SequenceMatcher(None, task_title, seen).ratio() > 0.8
                for seen in seen_titles
            ):
                continue

            new_tasks.append(task)
            seen_titles.append(task_title)

        state["seen_tasks"] = seen_titles[-MAX_SEEN_TASKS:]

    update_state(record_new_tasks)
    
    digest = ai_result.get("digest", "Nothing to report right now!")
    
    if new_tasks:
        tasks_str = ""
        for i, task in enumerate(new_tasks, 1):
            tasks_str += f"{i}. {task.get('title')} (Source: {task.get('source')})\n"
        digest += f"\n\n🚨 **NEW TASKS DETECTED** 🚨\n{tasks_str}\nShould I add these to Notion? If yes, reply with their priority (high/medium/low) and progress. If I should ignore any of them, let me know so I can learn!"
    if digest and digest != "Nothing to report right now!":
        try:
            with open(LATEST_DIGEST_FILE, "w") as f:
                f.write(digest)
        except Exception:
            pass
            
    await edit_progress(context, chat_id, msg.message_id, "Your digest is ready below.")
    await send_assistant_response(context, chat_id, digest, title="Today’s digest", reply_markup=section_keyboard("today"))
        
    # Ask to Compile Mega Study Guides
    topics = ai_result.get("topics", [])
    if topics:
        topics_str = "\n".join([f"- {t}" for t in topics])
        topic_msg = (
            "I detected upcoming assignments or tests for these topics:\n"
            f"{topics_str}\n\n"
            "Choose a topic below to compile a study guide."
        )
        try:
            from inline_keyboards import get_digest_topic_keyboard
            await context.bot.send_message(
                chat_id=chat_id,
                text=render_assistant_text(topic_msg, title="Study opportunities"),
                parse_mode=ParseMode.HTML,
                reply_markup=get_digest_topic_keyboard(topics),
            )
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=topic_msg)


@require_auth
async def bash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    action = (context.args[0].lower() if context.args else "")
    if not action or len(context.args) != 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Usage: <code>/bash health|uptime|memory|disk|services|ollama</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    msg = await context.bot.send_message(chat_id=chat_id, text=f"Running diagnostic: <code>{escape_html(action)}</code>…", parse_mode=ParseMode.HTML)
    output = await asyncio.to_thread(run_bash_safely, action, chat_id=chat_id)
    reply = f"<b>Diagnostic: {escape_html(action)}</b>\n\n<pre>{escape_html(output[:3600])}</pre>"
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=reply, parse_mode=ParseMode.HTML)
    except Exception:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=output[:3800])

@require_auth
async def priority_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if len(context.args) != 2:
        await context.bot.send_message(chat_id=chat_id, text="Usage: `/p <short_id> <high/medium/low>`", parse_mode="Markdown")
        return
        
    short_id, priority = context.args[0], context.args[1].lower()
    if priority not in ["high", "medium", "low"]:
        await context.bot.send_message(chat_id=chat_id, text="Priority must be high, medium, or low.")
        return

    state = load_state()
    task = _pending_task(state, short_id)
    if not task:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Could not find pending task with ID `{short_id}`.", parse_mode="Markdown")
        return

    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from scrapers.notion_client import update_notion_task
    
    if await asyncio.to_thread(update_notion_task, task["page_id"], priority=priority):
        _update_pending_task(short_id, priority=priority)
        await context.bot.send_message(chat_id=chat_id, text=f"✅ Task priority updated to **{priority.capitalize()}** in Notion!", parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=chat_id, text="❌ Failed to update Notion.")

@require_auth
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id

    if await _handle_ui_callback(update, context):
        return

    if data == "today:refresh":
        context.args = []
        await summary_command(update, context)
        return

    # ── Mode switcher callbacks ──────────────────────────────────────────
    if data.startswith("mode:set:"):
        mode = data.split(":", 2)[2]
        if mode in ("default", "tutor", "quick", "drill"):
            update_state(lambda s: s.setdefault("user_modes", {}).update({str(chat_id): mode}))
            from bot.ui import mode_keyboard
            await _edit_ui(
                query,
                f"✅ Chat mode updated to <b>{mode.capitalize()}</b>.\n\nYour next messages will use this style.",
                reply_markup=mode_keyboard(mode),
            )
        return

    # ── Model switcher callbacks ─────────────────────────────────────────
    if data.startswith("model:set:"):
        requested = data.split(":", 2)[2]
        mapping = {
            "auto": "auto",
            "local": "local",
            "flash": "flash",
            "pro": "pro",
            "llama3.3": "openrouter:meta-llama/llama-3.3-70b-instruct:free",
            "qwen-coder": "openrouter:qwen/qwen3-coder:free",
        }
        model_val = mapping.get(requested, requested)
        update_state(lambda s: s.setdefault("user_models", {}).update({str(chat_id): model_val}))
        from bot.ui import model_selection_keyboard
        display_map = {
            "auto": "Auto (Google + Local)",
            "local": "Local Cluster",
            "flash": "Gemini 3.7 Flash (AGY)",
            "pro": "Gemini 3.1 Pro (AGY)",
            "openrouter:meta-llama/llama-3.3-70b-instruct:free": "Llama 3.3 70B",
            "openrouter:qwen/qwen3-coder:free": "Qwen Coder",
        }
        display_name = display_map.get(model_val, model_val.replace("openrouter:", ""))
        await _edit_ui(
            query,
            f"✅ Active model updated to <b>{escape_html(display_name)}</b>.",
            reply_markup=model_selection_keyboard(model_val),
        )
        return

    # ── Chat response action chips ───────────────────────────────────────
    if data.startswith("chat_act:"):
        action = data.split(":")[1]
        from bot.ai_bridge import get_session_turns, send_to_antigravity_and_wait
        from bot.ui import begin_progress, edit_progress, chat_action_keyboard
        turns = get_session_turns(chat_id, max_turns=2)
        last_user = turns[-1]["user"] if turns else "previous question"
        last_bot = turns[-1]["assistant"] if turns else ""

        if action == "simpler":
            prompt = f"Please explain this simpler with an intuitive analogy or ELI5 breakdown:\n\n{last_bot[:1500]}"
            status_text = "💡 Simplifying explanation..."
        elif action == "testme":
            prompt = f"Based on this concept, give me 1 challenging practice question to test my understanding:\n\n{last_bot[:1500]}"
            status_text = "📝 Generating practice problem..."
        elif action == "regen":
            prompt = f"Please provide an alternative explanation or solution for: {last_user}"
            status_text = "🔄 Regenerating response..."
        else:
            prompt = last_user
            status_text = "Thinking..."

        status_msg = await begin_progress(context, chat_id, status_text)
        try:
            reply = await send_to_antigravity_and_wait(
                prompt,
                chat_id,
                context,
                status_msg,
                cloud_consent=True,
            )
            await edit_progress(context, chat_id, status_msg.message_id, "Your response is ready below.")
            await send_assistant_response(context, chat_id, reply, reply_markup=chat_action_keyboard())
        except Exception as exc:
            logger.error("Chat action failed: %s", exc)
            await edit_progress(context, chat_id, status_msg.message_id, "❌ Could not complete that action. Please try again.")
        return

    # ── Quick action commands ────────────────────────────────────────────
    if data == "cmd:summary":
        context.args = []
        await summary_command(update, context)
        return
    elif data == "cmd:ping":
        await _edit_ui(query, render_assistant_text(get_health_status(), title="Health"), reply_markup=section_keyboard("more"))
        return
    elif data == "cmd:stats":
        await _edit_ui(query, render_assistant_text(get_cost_summary(), title="Usage"), reply_markup=section_keyboard("more"))
        return
    elif data == "cmd:backup":
        await _edit_ui(query, "<b>Create a backup?</b>\n\nThis creates a new backup of the bot’s critical state files.", reply_markup=_issue_confirmation(context, "more:backup"))
        return
    elif data == "cmd:correlations":
        await _edit_ui(query, render_assistant_text(get_correlation_summary(), title="Correlations"), reply_markup=section_keyboard("more"))
        return

    # ── Digest topic guide builder ───────────────────────────────────────
    if data.startswith("build_guide:"):
        topic = data.split("build_guide:", 1)[1]
        try:
            from scrapers.mega_study_builder import generate_mega_guide
            await _edit_ui(query, f"<b>Building study guide</b>\n\nPreparing a guide for <b>{escape_html(topic)}</b>. This may take a minute.")
            loop = asyncio.get_running_loop()
            # Track the executor task to prevent fire-and-forget
            future = loop.run_in_executor(None, generate_mega_guide, topic)
            # Wrap the future in an async function for proper tracking
            async def wait_for_future():
                return await asyncio.wrap_future(future)
            result = await _track_task(asyncio.create_task(wait_for_future()))
            await send_assistant_response(context, chat_id, result, title="Study guide", reply_markup=section_keyboard("study"))
        except Exception:
            logger.exception("Study guide build failed")
            await _edit_ui(
                query,
                _error_screen("I couldn’t build that study guide", "Try again with a shorter or more specific topic."),
                reply_markup=section_keyboard("study"),
            )
        return
    elif data == "digest_dismiss":
        await _edit_ui(query, "<b>Study topics dismissed</b>\n\nYou can open Study whenever you’re ready.", reply_markup=section_keyboard("study"))
        return

    # ── Assignment calendar proposals ───────────────────────────────────
    if data.startswith("calendar:approve:") or data.startswith("calendar:reject:"):
        try:
            from scrapers.assignment_calendar import AssignmentCalendarService

            _prefix, decision, batch_id = data.split(":", 2)
            service = AssignmentCalendarService()
            if decision == "approve":
                applied = await asyncio.to_thread(service.approve_proposal, batch_id)
                text = f"Calendar proposal approved. Synced {applied} assignment event(s)."
            else:
                await asyncio.to_thread(service.reject_proposal, batch_id)
                text = "Calendar proposal declined. I will use that preference only for future suggestions."
            await _edit_ui(
                query,
                render_assistant_text(text, title="Calendar"),
                reply_markup=section_keyboard("calendar"),
            )
        except Exception:
            logger.exception("Calendar proposal decision failed")
            await _edit_ui(
                query,
                _error_screen("Calendar proposal failed", "The proposal may have expired. Open Calendar to preview current changes."),
                reply_markup=section_keyboard("calendar"),
            )
        return

    # ── Task priority buttons ────────────────────────────────────────────
    if data.startswith("task_prio:"):
        parts = data.split(":")
        if len(parts) == 3:
            tid, prio = parts[1], parts[2]
            try:
                from scrapers.notion_client import update_notion_task
                state = load_state()
                task = _pending_task(state, tid)
                if task and await asyncio.to_thread(update_notion_task, task["page_id"], priority=prio):
                    _update_pending_task(tid, priority=prio)
                    task["priority"] = prio
                    await query.edit_message_text(
                        _render_task_control(task, f"✅ Priority set to <b>{escape_html(prio.capitalize())}</b> in Notion."),
                        parse_mode=ParseMode.HTML,
                        reply_markup=get_task_actions_keyboard(tid),
                    )
                else:
                    await query.edit_message_text(
                        _error_screen("Task update failed", "The task may no longer be active. Refresh Today and try again."),
                        parse_mode=ParseMode.HTML,
                        reply_markup=home_keyboard(),
                    )
            except Exception:
                logger.exception("Task priority update failed")
                await query.edit_message_text(
                    _error_screen("Task update failed", "Try again in a moment."),
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_task_actions_keyboard(tid),
                )
        return
    elif data.startswith("task_status:"):
        parts = data.split(":")
        if len(parts) == 3:
            tid, status_key = parts[1], parts[2]
            status = {"in_progress": "In progress", "done": "Done"}.get(status_key)
            if not status:
                await query.edit_message_text(
                    _error_screen("Task status is unavailable", "Choose a valid task action or refresh Today."),
                    parse_mode=ParseMode.HTML,
                    reply_markup=home_keyboard(),
                )
                return
            try:
                from scrapers.notion_client import update_notion_task
                state = load_state()
                task = _pending_task(state, tid)
                if task and await asyncio.to_thread(update_notion_task, task["page_id"], status=status):
                    _update_pending_task(tid, status=status)
                    task["status"] = status
                    if status == "Done":
                        # The completed message stays as confirmation, while
                        # its stale controls are removed.
                        await query.edit_message_text(
                            _render_task_control(task, "✅ Marked Done in Notion."),
                            parse_mode=ParseMode.HTML,
                        )
                    else:
                        await query.edit_message_text(
                            _render_task_control(task, "▶️ Marked In progress in Notion."),
                            parse_mode=ParseMode.HTML,
                            reply_markup=get_task_actions_keyboard(tid),
                        )
                else:
                    await query.edit_message_text(
                        _error_screen("Task update failed", "The task may no longer be active. Refresh Today and try again."),
                        parse_mode=ParseMode.HTML,
                        reply_markup=home_keyboard(),
                    )
            except Exception:
                logger.exception("Task status update failed")
                await query.edit_message_text(
                    _error_screen("Task update failed", "Try again in a moment."),
                    parse_mode=ParseMode.HTML,
                    reply_markup=get_task_actions_keyboard(tid),
                )
        return
    elif data == "task_ignore_all":
        await query.edit_message_text("✅ All tasks ignored.")
        return

    # ── Photo response buttons ───────────────────────────────────────────
    if data == "photo:grade":
        await _edit_ui(
            query,
            "<b>Grade a practice photo</b>\n\nSend a clear photo of your completed work with a caption such as <code>SAT Math — check questions 1–10</code>.",
            reply_markup=section_keyboard("study"),
        )
        return
    elif data == "photo:save":
        await _edit_ui(
            query,
            "<b>Photo text saved</b>\n\nThe extracted text is already available to your next digest and follow-up questions.",
            reply_markup=section_keyboard("ask"),
        )
        return
    elif data == "photo:ask":
        await _edit_ui(
            query,
            "<b>Ask about this photo</b>\n\nReply to the photo result with your question, or send a new message describing what you want to know.",
            reply_markup=section_keyboard("ask"),
        )
        return

    # ── Legacy: build_guide_ (drive file) ────────────────────────────────
    if data.startswith("build_guide_"):
        file_id = data.split("build_guide_")[1]
        await _edit_ui(query, "<b>Building study guide</b>\n\nReading the selected file and preparing your guide. This may take a minute.")
        loop = asyncio.get_running_loop()
        try:
            from scrapers.mega_study_builder import build_guide_for_drive_file
            # Track the executor task to prevent fire-and-forget
            future = loop.run_in_executor(None, build_guide_for_drive_file, file_id, "XA_MWF Notes")
            # Wrap the future in an async function for proper tracking
            async def wait_for_future():
                return await asyncio.wrap_future(future)
            result = await _track_task(asyncio.create_task(wait_for_future()))
            await send_assistant_response(context, chat_id, result, title="Study guide", reply_markup=section_keyboard("study"))
        except Exception:
            logger.exception("Drive-file study guide build failed")
            await _edit_ui(
                query,
                _error_screen("I couldn’t build that study guide", "Try again after checking the source file is available."),
                reply_markup=section_keyboard("study"),
            )
        return

    await _edit_ui(
        query,
        _error_screen("That action has expired", "Open Home to continue."),
        reply_markup=home_keyboard(),
    )

@require_auth
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Health check: uptime, disk, last digest, queue size, file sizes."""
    await update.message.reply_text(render_assistant_text(get_health_status(), title="Health"), parse_mode=ParseMode.HTML)

@require_auth
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cost dashboard: LLM usage, tokens, estimated cost."""
    await update.message.reply_text(render_assistant_text(get_cost_summary(), title="Usage"), parse_mode=ParseMode.HTML)

@require_auth
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a backup now or list available backups."""
    await update.message.reply_text(
        "<b>Create a backup?</b>\n\nThis creates a new private backup of critical state files.",
        parse_mode=ParseMode.HTML,
        reply_markup=_issue_confirmation(context, "more:backup"),
    )

@require_auth
async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List or restore from backups. Usage: /restore [list|dry-run <path>]"""
    args = context.args
    if not args or args[0] == "list":
        backups = list_backups()
        if not backups:
            await update.message.reply_text("No backups found.")
            return
        lines = ["📦 **Available backups:**"]
        for b in backups:
            lines.append(f"  `{b['date']}` — {b['size_mb']}MB")
        lines.append("\nUse `/restore dry-run <path>` to preview restore.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    elif args[0] == "dry-run" and len(args) > 1:
        result = restore_backup(args[1], dry_run=True)
        await update.message.reply_text(result, parse_mode="Markdown")

@require_auth
async def correlations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show cross-source correlation stats."""
    await update.message.reply_text(get_correlation_summary(), parse_mode="Markdown")


@require_auth
async def classroom_pdfs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Download PDFs from Google Classroom assignments."""
    msg = await update.message.reply_text("📥 Downloading Classroom PDFs...")
    try:
        from scrapers.google_scraper import download_classroom_pdfs
        result = await asyncio.to_thread(download_classroom_pdfs, "classroom_pdfs")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=msg.message_id,
            text=result
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=msg.message_id,
            text=f"❌ Error downloading PDFs: {e}"
        )


@require_auth
async def canvas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current Canvas work and completion state without an LLM."""
    msg = await update.message.reply_text("<b>Checking Canvas coursework…</b>\n\nRetrieving assignments and completion status.", parse_mode=ParseMode.HTML)
    try:
        from scrapers.canvas_scraper import get_upcoming_assignments
        result = await asyncio.to_thread(get_upcoming_assignments)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=render_assistant_text(result, title="Canvas coursework"),
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("Canvas command failed")
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text="<b>I couldn’t check Canvas.</b>\n\nVerify the Canvas session is available, then try again.",
            parse_mode=ParseMode.HTML,
        )


@require_auth
async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Preview and control the disabled-by-default assignment calendar sync."""
    from scrapers.assignment_calendar import AssignmentCalendarService, CalendarSyncError, format_preview
    from inline_keyboards import get_calendar_proposal_keyboard

    args = [arg.lower() for arg in context.args]
    action = args[0] if args else "status"
    service = AssignmentCalendarService()
    try:
        if action == "status":
            status = await asyncio.to_thread(service.status)
            await update.message.reply_text(
                "Assignment calendar status\n"
                f"Enabled: {status['enabled']}\n"
                f"Local CalDAV configured: {status['caldav_configured']}\n"
                f"Google mirror configured: {status['google_configured']}\n"
                f"Tracked events: {status['tracked_events']}\n\n"
                "Use /calendar preview before enabling external writes."
            )
            return
        if action == "preview":
            message = await update.message.reply_text("Checking assignments without changing any calendar...")
            actions, batch_id, recommended = await asyncio.to_thread(service.preview)
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message.message_id,
                text=format_preview(actions, batch_id, recommended),
                reply_markup=get_calendar_proposal_keyboard(batch_id) if batch_id else None,
            )
            return
        if action == "enable":
            await update.message.reply_text(
                "Confirm enabling calendar sync:",
                reply_markup=_issue_confirmation(context, "cal:enable", cancel="nav:calendar"),
            )
            return
        if action == "disable":
            await update.message.reply_text(
                "Confirm disabling calendar sync:",
                reply_markup=_issue_confirmation(context, "cal:disable", cancel="nav:calendar"),
            )
            return
        if action == "sync":
            await update.message.reply_text(
                "Confirm syncing official calendar changes now:",
                reply_markup=_issue_confirmation(context, "cal:sync", cancel="nav:calendar"),
            )
            return
        await update.message.reply_text("Usage: /calendar [status|preview|enable|disable|sync]")
    except CalendarSyncError as exc:
        logger.warning("Calendar sync blocked: %s", exc)
        await update.message.reply_text("Calendar sync is blocked by its current configuration. Check Calendar status and try again.")
    except Exception as exc:
        logger.exception("Calendar command failed")
        await update.message.reply_text("Calendar command failed. Check the configured calendar service, then retry.")

@require_auth
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show an interactive help browser with the slash commands as fallbacks."""
    await update.message.reply_text(
        "<b>Help</b>\n\nChoose a category for a short, task-focused guide. "
        "All slash commands remain available as power-user shortcuts.",
        parse_mode=ParseMode.HTML,
        reply_markup=help_keyboard(),
    )


@require_auth
async def errors_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Scan bot logs for recent errors and report findings."""
    chat_id = update.effective_chat.id
    msg = await update.message.reply_text("🔍 Scanning logs for issues...")

    # Parse optional hours argument
    hours = 24
    if context.args:
        try:
            hours = int(context.args[0])
            hours = min(max(hours, 1), 168)
        except ValueError:
            pass

    try:
        import subprocess
        scanner = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "log_scanner.py")
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, scanner, "--hours", str(hours), "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 2:
            severity = "🚨"
        elif result.returncode == 0:
            severity = "✅"
        else:
            severity = "⚠️"

        data = json.loads(result.stdout)
        count = data["count"]
        errors = data["errors"]

        # Build summary by category
        from collections import Counter
        cats = Counter(m["category"] for m in errors)
        summary_parts = [f"{severity} **Log Scan ({hours}h): {count} issue(s)**"]
        summary_parts.append(f"```")
        cat_emoji = {
            "TELEGRAM_PARSE": "📝", "TELEGRAM_FAIL": "📝",
            "RESOURCE_WARN": "🔋", "SSL_LEAK": "🔌",
            "CSRF_WARN": "🔑", "AUTH_FAIL": "🔒", "API_QUOTA": "🚫",
            "FALLBACK_FAIL": "🤖", "OPENROUTER_FAIL": "🌐",
            "OLLAMA_FAIL": "🦙", "AI_HALLUCINATION": "🤪",
            "RECOVERY_AGENT": "🩺", "ALL_MODELS_FAIL": "💀",
            "TIMEOUT": "⏰", "TRACEBACK": "🔥", "DOWNLOAD_FAIL": "⬇️",
            "GUIDE_FAIL": "📚", "DIGEST_FAIL": "📊",
            "RATE_LIMIT": "🐢", "NETWORK_ERR": "🌍",
            "CMD_FAIL": "💻", "SCAN_ERR": "❓", "WATCHDOG_ERR": "👀",
        }
        for cat, n in cats.most_common():
            emoji = cat_emoji.get(cat, "❓")
            summary_parts.append(f"  {emoji} {cat}: {n}")
        summary_parts.append("```")

        # Show top 5 most interesting errors (non-ResourceWarning first)
        interesting = [e for e in errors if e["category"] not in ("RESOURCE_WARN",)]
        if not interesting:
            interesting = errors[:3]

        for e in interesting[:5]:
            ts = e.get("timestamp", "?")[:19]
            msg_text = e["message"][:250]
            summary_parts.append(f"\n`{ts}` [{e['category']}]\n{msg_text}")

        if len(errors) > 5:
            summary_parts.append(f"\n_... and {len(errors) - 5} more issues. Use /errors <hours> to go deeper._")

        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg.message_id,
            text="\n".join(summary_parts)[:4000],
            parse_mode="Markdown"
        )
    except Exception as e:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=msg.message_id,
            text=f"❌ Log scan failed: {e}"
        )

async def _get_server_overview():
    try:
        import subprocess
        res = (await asyncio.to_thread(subprocess.check_output, ["uptime"], text=True)).strip()
        return f"🖥️ **Server Overview**\n`{res}`"
    except Exception as e: return str(e)

async def _get_mc_status():
    try:
        import subprocess
        try:
            res = (await asyncio.to_thread(
                subprocess.check_output, ["systemctl", "is-active", "minecraft"], text=True
            )).strip()
        except subprocess.CalledProcessError:
            res = "inactive"
        return f"⛏️ **Minecraft Server**\nStatus: `{res}`"
    except Exception as e: return str(e)

async def _get_embed_status():
    try:
        import os
        log_path = "/tmp/embed_build4.log"
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                lines = f.readlines()
            res = "".join(lines[-10:]).strip()
        else:
            res = "No log found"
        return f"🧠 **Embedding Progress**\n```\n{res}\n```"
    except Exception as e: return str(e)

async def _get_bot_status():
    try:
        import subprocess
        res = await asyncio.to_thread(
            subprocess.check_output, ["systemctl", "status", "antigravity-bot"], text=True
        )
        res = "\n".join(res.splitlines()[:5]).strip()
        return f"🤖 **Bot Service**\n```\n{res}\n```"
    except Exception as e: return str(e)

async def _get_mc_log():
    try:
        import subprocess
        res = (await asyncio.to_thread(
            subprocess.check_output,
            ["journalctl", "-u", "minecraft", "-n", "10", "--no-pager"],
            text=True,
        )).strip()
        return f"📜 **MC Logs**\n```\n{res}\n```"
    except Exception as e: return str(e)

async def _get_ram_status():
    try:
        import subprocess
        res = (await asyncio.to_thread(subprocess.check_output, ["free", "-h"], text=True)).strip()
        return f"💾 **RAM Usage**\n```\n{res}\n```"
    except Exception as e: return str(e)

async def _get_services_status():
    try:
        import subprocess
        res = await asyncio.to_thread(
            subprocess.check_output,
            ["systemctl", "list-units", "--type=service", "--state=running"],
            text=True,
        )
        res = "\n".join(res.splitlines()[:10]).strip()
        return f"⚙️ **Services**\n```\n{res}\n```"
    except Exception as e: return str(e)

async def _get_activity_feed():
    from activity_log import get_recent_events, format_events
    events = get_recent_events(10)
    return f"📈 **Activity Feed**\n{format_events(events)}"

async def _get_bot_log():
    try:
        import subprocess
        res = (await asyncio.to_thread(
            subprocess.check_output,
            ["journalctl", "-u", "antigravity-bot", "-n", "10", "--no-pager"],
            text=True,
        )).strip()
        return f"🤖 **Bot Logs**\n```\n{res}\n```"
    except Exception as e: return str(e)

async def _mc_start():
    try:
        import subprocess
        await asyncio.to_thread(subprocess.check_output, ["sudo", "systemctl", "start", "minecraft"])
        return "✅ Minecraft server starting..."
    except Exception as e: return str(e)

async def _mc_stop():
    try:
        import subprocess
        await asyncio.to_thread(subprocess.check_output, ["sudo", "systemctl", "stop", "minecraft"])
        return "🛑 Minecraft server stopping..."
    except Exception as e: return str(e)

@require_auth
async def server_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        help_txt = (
            "🎛️ **Server Dashboard**\n"
            "Usage: `/server <module>`\n\n"
            "Modules:\n"
            "• `overview` - Uptime & load\n"
            "• `ram` - Memory usage\n"
            "• `mc` - Minecraft status\n"
            "• `mcstart` / `mcstop` - Start/Stop MC\n"
            "• `mclog` - Minecraft latest logs\n"
            "• `bot` - Bot service status\n"
            "• `botlog` - Bot latest logs\n"
            "• `embed` - Embedding job status\n"
            "• `services` - Top running services\n"
            "• `activity` - Recent bot activity feed"
        )
        await update.message.reply_text(help_txt, parse_mode="Markdown")
        return
        
    cmd = args[0].lower()
    mapping = {
        "overview": _get_server_overview,
        "mc": _get_mc_status,
        "embed": _get_embed_status,
        "bot": _get_bot_status,
        "mclog": _get_mc_log,
        "ram": _get_ram_status,
        "services": _get_services_status,
        "activity": _get_activity_feed,
        "botlog": _get_bot_log,
        "mcstart": _mc_start,
        "mcstop": _mc_stop
    }
    
    if cmd in mapping:
        result = await mapping[cmd]()
        await update.message.reply_text(result, parse_mode="Markdown")
    else:
        await update.message.reply_text(f"❌ Unknown module: {cmd}")

# ── Import unified modules ───────────────────────────────────────────────
from llm_router import call_openrouter, get_cost_summary, is_valid_response, OR_DEFAULT_MODEL, OR_FALLBACK_MODEL
from utils import (
    run_bash_safely, enforce_all_rotations, create_backup,
    get_health_status, get_correlation_summary, correlate_items,
    restore_backup, list_backups,
)
from inline_keyboards import (
    get_new_tasks_keyboard, get_task_actions_keyboard, get_digest_topic_keyboard,
    get_study_guide_keyboard, get_photo_response_keyboard,
    get_quick_actions_keyboard,
)
from voice_handler import transcribe_voice

@require_auth
async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List pending Notion tasks with due dates, courses, and priorities."""
    from scrapers.notion_client import get_pending_notion_tasks, priority_emoji
    from bot.ui import begin_progress, edit_progress

    chat_id = update.effective_chat.id
    msg = await begin_progress(context, chat_id, "Pulling current tasks from Notion...")

    try:
        tasks = await asyncio.to_thread(get_pending_notion_tasks, 25)
        if not tasks:
            await edit_progress(
                context,
                chat_id,
                msg.message_id,
                "📋 **Notion Tasks**\n\nYou have no pending tasks in Notion! Great job.",
            )
            return

        lines = ["📋 **Your Pending Notion Tasks:**\n"]
        for idx, t in enumerate(tasks, 1):
            p_emoji = priority_emoji(t.get("priority", "medium"))
            due_str = f" • Due: {t['due_date']}" if t.get("due_date") else ""
            course_str = f" [{t['course']}]" if t.get("course") and t["course"] not in {"General", "Notion", ""} else ""
            status_str = f" ({t['status']})" if t.get("status") and t["status"] != "Not started" else ""
            lines.append(f"{idx}. {p_emoji} **{t['title']}**{course_str}{due_str}{status_str}")

        lines.append("\n_Use the dashboard buttons or /summary to manage your tasks._")
        await edit_progress(
            context,
            chat_id,
            msg.message_id,
            "\n".join(lines),
        )
    except Exception as e:
        logger.exception("Failed to pull Notion tasks: %s", e)
        await edit_progress(
            context,
            chat_id,
            msg.message_id,
            f"❌ Could not pull Notion tasks: {e}",
        )


notion_command = tasks_command
