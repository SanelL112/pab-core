import os
import sys
import json
import asyncio
import subprocess
import logging
from telegram import Update
from telegram.ext import ContextTypes
from bot.security import require_auth
from bot.state import load_state, update_state
from utils import create_backup, get_correlation_summary, get_health_status, list_backups, restore_backup, run_bash_safely
from config import SANEL_CHAT_ID, GROUPME_GROUP_ID, LATEST_DIGEST_FILE, MAX_SEEN_TASKS
from bot.runtime import _track_task
from scrapers.mega_study_builder import build_guide_for_drive_file
from llm_router import get_cost_summary
import time

logger = logging.getLogger(__name__)


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

    priority = task.get("priority", "medium")
    source = task.get("source") or "Unknown source"
    if task.get("course"):
        source += f" • {task['course']}"
    text = (
        f"{priority_emoji(priority)} **{task.get('title', 'Untitled task')}**\n"
        f"📌 {source}\n"
        f"📅 Due: {task.get('due_date') or 'No due date'}\n"
        f"Priority: **{priority.capitalize()}**\n"
        f"Status: **{task.get('status', 'Not started')}**"
    )
    return text + (f"\n\n{confirmation}" if confirmation else "\n\nChoose a priority or update its status:")

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
    valid_local = ["auto", "flash", "pro"]
    
    if not args:
        state = load_state()
        current = state["user_models"].get(str(chat_id), "auto")
        display_current = current.replace("openrouter:", "") if current.startswith("openrouter:") else current
        alias_list = " | ".join([f"`/model {k}`" for k in FREE_ALIASES.keys()])
        await update.message.reply_text(
            f"Current model: *{display_current}*\n\n"
            f"*Smart Routing:* `/model auto` (Auto-detects PII and routes to Free models or Private models)\n"
            f"*Private (G1) Models:* `/model flash` | `/model pro`\n"
            f"*Free OpenRouter Models:* {alias_list}\n\n"
            f"_(Note: OpenRouter endpoints are strictly hardcoded to the free tier to guarantee zero charges)_",
            parse_mode="Markdown"
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
    msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Generating your summary digest... This might take a minute.")
    
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
        return canvas, classroom, gmail, groupme, announcements, docs
    
    try:
        canvas, classroom, gmail, groupme, announcements, docs = await asyncio.to_thread(gather_all_data)
        ai_result = await asyncio.to_thread(process_all_sources, canvas, classroom, gmail, groupme, announcements, docs)
    except Exception as e:
        logger.error(f"Error during AI digest generation: {e}")
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=f"❌ The AI timed out or crashed while generating your digest: {e}")
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
            
    await context.bot.delete_message(chat_id=chat_id, message_id=msg.message_id)
    from utils import sanitize_markdown
    safe_digest = sanitize_markdown(digest)
    try:
        await context.bot.send_message(chat_id=chat_id, text=f"📊 **On-Demand Digest**\n\n{safe_digest}", parse_mode="Markdown")
    except Exception:
        await context.bot.send_message(chat_id=chat_id, text=f"📊 **On-Demand Digest**\n\n{digest}")
        
    # Ask to Compile Mega Study Guides
    topics = ai_result.get("topics", [])
    if topics:
        topics_str = "\n".join([f"- {t}" for t in topics])
        safe_topics_str = sanitize_markdown(topics_str)
        topic_msg = (
            f"🧠 **I detected you have upcoming assignments/tests for the following topics:**\n"
            f"{safe_topics_str}\n\n"
            f"Would you like me to compile a Mega Study Guide for any of these? (Just reply 'Build a guide for...') 📚"
        )
        try:
            await context.bot.send_message(chat_id=chat_id, text=topic_msg, parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(chat_id=chat_id, text=topic_msg)


@require_auth
async def bash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cmd = " ".join(context.args)
    if not cmd:
        await context.bot.send_message(chat_id=chat_id, text="Usage: `/bash <command>`", parse_mode="Markdown")
        return

    msg = await context.bot.send_message(chat_id=chat_id, text=f"💻 Running: `{cmd}`...", parse_mode="Markdown")
    output = await asyncio.to_thread(run_bash_safely, cmd, chat_id=chat_id)
    reply = "💻 **`" + cmd[:100] + "`**\n\n```\n" + output[:3800] + "\n```"
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=reply, parse_mode="Markdown")
    except Exception:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=msg.message_id, text=reply)

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

    # ── Quick action commands ────────────────────────────────────────────
    if data == "cmd:summary":
        context.args = []
        await summary_command(update, context)
        return
    elif data == "cmd:ping":
        await query.edit_message_text(get_health_status(), parse_mode="Markdown")
        return
    elif data == "cmd:stats":
        await query.edit_message_text(get_cost_summary(), parse_mode="Markdown")
        return
    elif data == "cmd:backup":
        path = await asyncio.to_thread(create_backup)
        await query.edit_message_text(
            f"✅ Backup created: `{os.path.basename(path)}`" if path else "❌ Backup failed"
        )
        return
    elif data == "cmd:correlations":
        await query.edit_message_text(get_correlation_summary(), parse_mode="Markdown")
        return

    # ── Digest topic guide builder ───────────────────────────────────────
    if data.startswith("build_guide:"):
            topic = data.split("build_guide:", 1)[1]
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=query.message.message_id,
                text=f"🔨 Building Mega Study Guide for: **{topic}**... This will take a minute!"
            )
            try:
                from scrapers.mega_study_builder import generate_mega_guide
                loop = asyncio.get_running_loop()
                # Track the executor task to prevent fire-and-forget
                future = loop.run_in_executor(None, generate_mega_guide, topic)
                # Wrap the future in an async function for proper tracking
                async def wait_for_future():
                    return await asyncio.wrap_future(future)
                result = await _track_task(asyncio.create_task(wait_for_future()))
                from utils import sanitize_markdown
                safe_result = sanitize_markdown(result)
                try:
                    await context.bot.send_message(chat_id=chat_id, text=safe_result, parse_mode="Markdown")
                except Exception:
                    await context.bot.send_message(chat_id=chat_id, text=result)
            except Exception as e:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed to build guide: {e}")
            return
    elif data == "digest_dismiss":
        await query.edit_message_text("👌 Okay, I won't build a guide right now. Ask me anytime!")
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
                        _render_task_control(task, f"✅ Priority set to **{prio.capitalize()}** in Notion."),
                        parse_mode="Markdown",
                        reply_markup=get_task_actions_keyboard(tid),
                    )
                else:
                    await query.edit_message_text(f"❌ Could not update `{tid}`")
            except Exception as e:
                await query.edit_message_text(f"❌ Error: {e}")
        return
    elif data.startswith("task_status:"):
        parts = data.split(":")
        if len(parts) == 3:
            tid, status_key = parts[1], parts[2]
            status = {"in_progress": "In progress", "done": "Done"}.get(status_key)
            if not status:
                await query.edit_message_text("❌ Unknown task status.")
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
                            parse_mode="Markdown",
                        )
                    else:
                        await query.edit_message_text(
                            _render_task_control(task, "▶️ Marked In progress in Notion."),
                            parse_mode="Markdown",
                            reply_markup=get_task_actions_keyboard(tid),
                        )
                else:
                    await query.edit_message_text(f"❌ Could not update `{tid}`")
            except Exception as e:
                await query.edit_message_text(f"❌ Error: {e}")
        return
    elif data == "task_ignore_all":
        await query.edit_message_text("✅ All tasks ignored.")
        return

    # ── Photo response buttons ───────────────────────────────────────────
    if data == "photo:grade":
        await query.edit_message_text("📝 To grade a practice test, send a photo of your completed problems with the topic as a caption (e.g. 'SAT Math')")
        return
    elif data == "photo:save":
        await query.edit_message_text("✅ Got it — I'll save any photo text I see to your extracts.")
        return
    elif data == "photo:ask":
        await query.edit_message_text("💬 Ask me anything! Just reply to the photo text with your question.")
        return

    # ── Legacy: build_guide_ (drive file) ────────────────────────────────
    if data.startswith("build_guide_"):
        file_id = data.split("build_guide_")[1]
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=query.message.message_id,
            text="⏳ Downloading PDF, reading handwriting via Gemini Vision, and building Mega Study Guide... This will take a minute!"
        )
        loop = asyncio.get_running_loop()
        try:
            from scrapers.mega_study_builder import build_guide_for_drive_file
            # Track the executor task to prevent fire-and-forget
            future = loop.run_in_executor(None, build_guide_for_drive_file, file_id, "XA_MWF Notes")
            # Wrap the future in an async function for proper tracking
            async def wait_for_future():
                return await asyncio.wrap_future(future)
            result = await _track_task(asyncio.create_task(wait_for_future()))
            from utils import sanitize_markdown
            safe_result = sanitize_markdown(result)
            try:
                await context.bot.send_message(chat_id=chat_id, text=safe_result, parse_mode="Markdown")
            except Exception:
                await context.bot.send_message(chat_id=chat_id, text=result)
        except Exception as e:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ Failed to build guide: {e}")

@require_auth
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Health check: uptime, disk, last digest, queue size, file sizes."""
    await update.message.reply_text(get_health_status(), parse_mode="Markdown")

@require_auth
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cost dashboard: LLM usage, tokens, estimated cost."""
    await update.message.reply_text(get_cost_summary(), parse_mode="Markdown")

@require_auth
async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a backup now or list available backups."""
    msg = await update.message.reply_text("📦 Creating backup...")
    path = await asyncio.to_thread(create_backup)
    if path:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=msg.message_id,
            text=f"✅ Backup created: `{os.path.basename(path)}`"
        )
    else:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id, message_id=msg.message_id,
            text="❌ Backup failed. Check logs."
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
    msg = await update.message.reply_text("🎯 Checking Canvas coursework and completion status...")
    try:
        from scrapers.canvas_scraper import get_upcoming_assignments
        result = await asyncio.to_thread(get_upcoming_assignments)
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=result,
            parse_mode="Markdown",
        )
    except Exception as exc:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            text=f"⚠️ Canvas check failed: {exc}",
        )

@require_auth
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all available commands organized by category."""
    help_text = (
        "🤖 **Antigravity Bot Commands**\n\n"
        "**Core & Assistant**\n"
        "• `/help` - Show this menu\n"
        "• `/summary` - Manual data digest trigger\n"
        "• `/canvas` - Missing work, due-soon work, and completion status\n"
        "• `/models` - List & switch AI models\n"
        "• `/bash <cmd>` - Run bash commands directly\n"
        "• `/p <num>` - Adjust bot priority queue\n\n"
        "**Server Management**\n"
        "• `/server` - Interactive Server Dashboard\n"
        "• `/ping` - Health check & uptime stats\n"
        "• `/stats` - Token & LLM cost usage dashboard\n\n"
        "**Data & Memory**\n"
        "• `/backup` - Create an immediate brain backup\n"
        "• `/restore` - List & restore backups\n"
        "• `/correlations` - Cross-source data correlation stats\n"
        "• `/classroom` - Download PDFs from Google Classroom\n"
        "• `/errors` - Scan logs for recent errors"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


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

# Track bot start time for /ping
BOT_START_TIME = time.time()

# ── Entry point ────────────────────────────────────────────────────────────────
