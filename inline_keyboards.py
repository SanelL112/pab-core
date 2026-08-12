"""
inline_keyboards.py — Feature 9: Quick-action inline keyboards for digests, tasks, and study guides.

Provides Telegram InlineKeyboardMarkup for common actions so the user doesn't need to
type responses or parse intent through the LLM.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import Optional


def _callback_data(prefix: str, value: str, *, limit: int = 64) -> str:
    """Build callback data within Telegram's 64-byte limit.

    Topic labels come from external sources and may be unexpectedly long or
    contain multi-byte characters.  Truncating at a UTF-8 boundary keeps the
    legacy guide callback usable instead of causing Telegram to reject the
    entire keyboard.
    """
    available = limit - len(prefix.encode("utf-8"))
    encoded = str(value).encode("utf-8")[:max(0, available)]
    return prefix + encoded.decode("utf-8", errors="ignore")


def get_new_tasks_keyboard(task_ids: list) -> InlineKeyboardMarkup:
    """
    Keyboard for new tasks detected in digest.
    task_ids: list of short task identifiers.

    Buttons: [High] [Medium] [Low] [Ignore All]
    """
    buttons = []
    for tid in task_ids[:5]:  # Max 5 at a time
        buttons.append([
            InlineKeyboardButton(f"🔴 {tid} - High", callback_data=_callback_data("task_prio:", f"{tid}:high")),
            InlineKeyboardButton(f"🟡 {tid} - Medium", callback_data=_callback_data("task_prio:", f"{tid}:medium")),
            InlineKeyboardButton(f"🔽 {tid} - Low", callback_data=_callback_data("task_prio:", f"{tid}:low")),
        ])
    buttons.append([
        InlineKeyboardButton("✅ Ignore All", callback_data="task_ignore_all"),
    ])
    return InlineKeyboardMarkup(buttons)


def get_task_actions_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """Controls for one newly synced Notion task.

    One task per Telegram message means choosing a priority does not hide the
    controls for every other task in that digest.
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔴 High", callback_data=_callback_data("task_prio:", f"{task_id}:high")),
            InlineKeyboardButton("🟡 Medium", callback_data=_callback_data("task_prio:", f"{task_id}:medium")),
            InlineKeyboardButton("🔵 Low", callback_data=_callback_data("task_prio:", f"{task_id}:low")),
        ],
        [
            InlineKeyboardButton("▶️ Start", callback_data=_callback_data("task_status:", f"{task_id}:in_progress")),
            InlineKeyboardButton("✅ Done", callback_data=_callback_data("task_status:", f"{task_id}:done")),
        ],
    ])


def get_calendar_proposal_keyboard(batch_id: str) -> InlineKeyboardMarkup:
    """Approve or reject one stored batch of non-official calendar tasks."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Add proposed tasks", callback_data=f"calendar:approve:{batch_id}"),
            InlineKeyboardButton("Keep out of calendar", callback_data=f"calendar:reject:{batch_id}"),
        ],
    ])


def get_digest_topic_keyboard(topics: list) -> InlineKeyboardMarkup:
    """
    Keyboard for detected study topics.
    topics: list of topic strings.

    Buttons: [Build Guide: Topic 1] [Build Guide: Topic 2] [Dismiss]
    """
    buttons = []
    for topic in topics[:4]:
        display_topic = str(topic)
        buttons.append([
            InlineKeyboardButton(
                f"🛠 Build Guide: {display_topic[:48]}",
                callback_data=_callback_data("build_guide:", display_topic),
            ),
        ])
    buttons.append([
        InlineKeyboardButton("❌ Dismiss", callback_data="digest_dismiss"),
    ])
    return InlineKeyboardMarkup(buttons)


def get_study_guide_keyboard(guide_name: str) -> InlineKeyboardMarkup:
    """
    Keyboard sent with a generated study guide.

    Buttons: [Grade Practice Test] [Schedule Session] [Share to Obsidian]
    """
    buttons = [
        [
            InlineKeyboardButton("📝 Grade Practice Photo", callback_data=_callback_data("grade_guide:", guide_name)),
            InlineKeyboardButton("📅 Schedule Study Time", callback_data=_callback_data("schedule_guide:", guide_name)),
        ],
        [
            InlineKeyboardButton("📝 Open in Obsidian", callback_data=_callback_data("obsidian_guide:", guide_name)),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def get_quick_actions_keyboard() -> InlineKeyboardMarkup:
    """
    Keyboard sent on /start or when user says "help".

    Buttons: [Summary] [Ping] [Stats] [Backup Now]
    """
    buttons = [
        [
            InlineKeyboardButton("📊 Digest", callback_data="cmd:summary"),
            InlineKeyboardButton("🏥 Health", callback_data="cmd:ping"),
        ],
        [
            InlineKeyboardButton("💰 Stats", callback_data="cmd:stats"),
            InlineKeyboardButton("💾 Backup", callback_data="cmd:backup"),
        ],
        [
            InlineKeyboardButton("📊 Correlations", callback_data="cmd:correlations"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def get_photo_response_keyboard() -> InlineKeyboardMarkup:
    """
    Keyboard sent after photo processing (OCR).

    Buttons: [Grade This] [Save to Notes] [Ask Question]
    """
    buttons = [
        [
            InlineKeyboardButton("📝 Grade Practice Test", callback_data="photo:grade"),
            InlineKeyboardButton("📋 Save to Notes", callback_data="photo:save"),
        ],
        [
            InlineKeyboardButton("❓ Ask Me About This", callback_data="photo:ask"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)
