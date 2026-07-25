"""
inline_keyboards.py — Feature 9: Quick-action inline keyboards for digests, tasks, and study guides.

Provides Telegram InlineKeyboardMarkup for common actions so the user doesn't need to
type responses or parse intent through the LLM.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from typing import Optional


def get_new_tasks_keyboard(task_ids: list) -> InlineKeyboardMarkup:
    """
    Keyboard for new tasks detected in digest.
    task_ids: list of short task identifiers.

    Buttons: [High] [Medium] [Low] [Ignore All]
    """
    buttons = []
    for tid in task_ids[:5]:  # Max 5 at a time
        buttons.append([
            InlineKeyboardButton(f"🔴 {tid} - High", callback_data=f"task_prio:{tid}:high"),
            InlineKeyboardButton(f"🟡 {tid} - Medium", callback_data=f"task_prio:{tid}:medium"),
            InlineKeyboardButton(f"🔽 {tid} - Low", callback_data=f"task_prio:{tid}:low"),
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
            InlineKeyboardButton("🔴 High", callback_data=f"task_prio:{task_id}:high"),
            InlineKeyboardButton("🟡 Medium", callback_data=f"task_prio:{task_id}:medium"),
            InlineKeyboardButton("🔵 Low", callback_data=f"task_prio:{task_id}:low"),
        ],
        [
            InlineKeyboardButton("▶️ Start", callback_data=f"task_status:{task_id}:in_progress"),
            InlineKeyboardButton("✅ Done", callback_data=f"task_status:{task_id}:done"),
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
        buttons.append([
            InlineKeyboardButton(f"🛠 Build Guide: {topic}", callback_data=f"build_guide:{topic}"),
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
            InlineKeyboardButton("📝 Grade Practice Photo", callback_data=f"grade_guide:{guide_name}"),
            InlineKeyboardButton("📅 Schedule Study Time", callback_data=f"schedule_guide:{guide_name}"),
        ],
        [
            InlineKeyboardButton("📝 Open in Obsidian", callback_data=f"obsidian_guide:{guide_name}"),
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
