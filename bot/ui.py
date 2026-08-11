"""Shared Telegram presentation primitives for the assistant interface.

This module keeps user-facing formatting and navigation out of command
handlers.  Dynamic and model-generated text is escaped before it is sent with
Telegram HTML parse mode, preventing malformed responses from breaking a
message or being interpreted as markup.
"""
from __future__ import annotations

from html import escape as _html_escape
import re
from typing import Iterable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction, ParseMode


# Telegram's hard limit is 4096 characters.  Keeping generated chunks smaller
# leaves room for HTML tags added by the renderer.
SAFE_MESSAGE_LENGTH = 2300


def escape_html(value: object) -> str:
    """Escape untrusted text for Telegram's HTML parse mode."""
    return _html_escape(str(value or ""), quote=False)


def _format_inline(value: str) -> str:
    """Apply only bounded inline formatting to untrusted generated text.

    A response can contain thousands of inline-code or italic markers.  Those
    HTML tags expand considerably, which could push an otherwise valid chunk
    past Telegram's 4096-character limit.  Headings, bullets, and fenced code
    blocks still provide hierarchy; bold is the only compact inline upgrade.
    """
    escaped = escape_html(value)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return escaped


def render_assistant_text(value: str, *, title: str | None = None) -> str:
    """Render an assistant response as safe, lightly structured HTML."""
    lines: list[str] = []
    in_code = False
    code_lines: list[str] = []

    for raw_line in (value or "").replace("\r\n", "\n").split("\n"):
        stripped = raw_line.strip()
        if stripped.startswith("```"):
            if in_code:
                lines.append(f"<pre>{escape_html(chr(10).join(code_lines))}</pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue

        if in_code:
            code_lines.append(raw_line)
            continue

        heading = re.match(r"^#{1,6}\s+(.+)$", stripped)
        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        numbered = re.match(r"^(\d+[.)])\s+(.+)$", stripped)
        quote = re.match(r"^>\s?(.+)$", stripped)
        if heading:
            lines.append(f"<b>{_format_inline(heading.group(1))}</b>")
        elif bullet:
            lines.append(f"• {_format_inline(bullet.group(1))}")
        elif numbered:
            lines.append(f"{escape_html(numbered.group(1))} {_format_inline(numbered.group(2))}")
        elif quote:
            lines.append(f"<blockquote>{_format_inline(quote.group(1))}</blockquote>")
        else:
            lines.append(_format_inline(raw_line))

    if in_code:
        lines.append(f"<pre>{escape_html(chr(10).join(code_lines))}</pre>")

    body = "\n".join(lines).strip() or "I don’t have a response to show yet."
    return f"<b>{escape_html(title)}</b>\n\n{body}" if title else body


def _split_large_unit(unit: str, limit: int) -> list[str]:
    """Break one unit on line/word boundaries without dropping content."""
    if len(unit) <= limit:
        return [unit]

    chunks: list[str] = []
    current = ""
    for line in unit.splitlines(keepends=True) or [unit]:
        if len(line) > limit:
            words = re.findall(r"\S+\s*|\s+", line)
            for word in words:
                if len(current) + len(word) > limit and current:
                    chunks.append(current.rstrip())
                    current = ""
                while len(word) > limit:
                    if current:
                        chunks.append(current.rstrip())
                        current = ""
                    chunks.append(word[:limit])
                    word = word[limit:]
                current += word
        elif len(current) + len(line) > limit and current:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return chunks or [unit[:limit]]


def _content_units(value: str, limit: int) -> Iterable[str]:
    """Yield paragraphs and complete fenced code blocks as independent units."""
    normal: list[str] = []
    code: list[str] = []
    in_code = False

    def flush_normal() -> Iterable[str]:
        nonlocal normal
        text = "".join(normal).strip("\n")
        normal = []
        if not text:
            return []
        paragraphs = re.split(r"\n{2,}", text)
        return [part for paragraph in paragraphs for part in _split_large_unit(paragraph, limit)]

    for line in (value or "").replace("\r\n", "\n").splitlines(keepends=True):
        if line.strip().startswith("```"):
            if not in_code:
                yield from flush_normal()
                code = [line]
                in_code = True
            else:
                code.append(line)
                unit = "".join(code).rstrip("\n")
                if len(unit) <= limit:
                    yield unit
                else:
                    code_body = "".join(code[1:-1])
                    for part in _split_large_unit(code_body, max(1, limit - 10)):
                        yield f"```\n{part}\n```"
                code = []
                in_code = False
            continue
        if in_code:
            code.append(line)
        else:
            normal.append(line)

    if in_code:
        code_body = "".join(code[1:])
        for part in _split_large_unit(code_body, max(1, limit - 10)):
            yield f"```\n{part}\n```"
    else:
        yield from flush_normal()


def paginate_text(value: str, *, limit: int = SAFE_MESSAGE_LENGTH) -> list[str]:
    """Split a generated response at readable boundaries before HTML rendering."""
    chunks: list[str] = []
    current = ""
    for unit in _content_units(value, limit):
        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(unit) > limit and current:
            chunks.append(current)
            current = unit
        else:
            current += separator + unit
    if current:
        chunks.append(current)
    return chunks or [""]


async def send_assistant_response(context, chat_id: int, text: str, *, title: str = "Assistant", reply_markup=None) -> None:
    """Send a full model response in valid, readable Telegram HTML chunks."""
    chunks = paginate_text(text)
    for index, chunk in enumerate(chunks):
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=render_assistant_text(chunk, title=title if index == 0 else None),
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )
        except Exception:
            # A final plain-text fallback means a model response is never lost
            # because a provider emitted unexpected markup.
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
            )


async def begin_progress(context, chat_id: int, text: str, *, action: str = ChatAction.TYPING):
    """Show an immediate chat action and an editable progress message."""
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=action)
    except Exception:
        pass
    return await context.bot.send_message(
        chat_id=chat_id,
        text=render_assistant_text(text, title="Working"),
        parse_mode=ParseMode.HTML,
    )


async def edit_progress(context, chat_id: int, message_id: int, text: str, *, reply_markup=None) -> None:
    """Update a progress message with safe, consistent presentation."""
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=render_assistant_text(text),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Today", callback_data="nav:today"),
            InlineKeyboardButton("💬 Ask", callback_data="nav:ask"),
        ],
        [
            InlineKeyboardButton("📚 Study", callback_data="nav:study"),
            InlineKeyboardButton("🗓 Calendar", callback_data="nav:calendar"),
        ],
        [InlineKeyboardButton("☰ More", callback_data="nav:more")],
    ])


def home_text(pending_tasks: int = 0) -> str:
    task_line = (
        f"<b>{pending_tasks}</b> task{'s' if pending_tasks != 1 else ''} waiting for an update."
        if pending_tasks else "No task actions are waiting right now."
    )
    return (
        "<b>Personal Assistant</b>\n"
        "<i>Your command center for school, planning, and quick help.</i>\n\n"
        f"<b>Today</b>\n{task_line}\n"
        "Automation is active; I’ll continue monitoring your connected sources.\n\n"
        "Choose an area below, or send a message in your own words."
    )


def navigation_keyboard(*, back: str = "nav:home", home: bool = True) -> list[list[InlineKeyboardButton]]:
    buttons = [InlineKeyboardButton("‹ Back", callback_data=back)]
    if home and back != "nav:home":
        buttons.append(InlineKeyboardButton("⌂ Home", callback_data="nav:home"))
    return [buttons]


def section_keyboard(section: str) -> InlineKeyboardMarkup:
    layouts = {
        "today": [
            [InlineKeyboardButton("↻ Refresh digest", callback_data="today:refresh")],
            [InlineKeyboardButton("🗓 Open calendar", callback_data="nav:calendar")],
        ],
        "ask": [
            [InlineKeyboardButton("🎙 Send a voice note", callback_data="ask:voice")],
            [InlineKeyboardButton("📷 Send a photo", callback_data="ask:photo")],
        ],
        "study": [
            [InlineKeyboardButton("📊 Check assignments", callback_data="study:assignments")],
            [InlineKeyboardButton("📝 Grade practice photo", callback_data="study:grade")],
            [InlineKeyboardButton("📚 Build a guide", callback_data="study:guide")],
        ],
        "calendar": [
            [
                InlineKeyboardButton("Status", callback_data="cal:status"),
                InlineKeyboardButton("Preview", callback_data="cal:preview"),
            ],
            [
                InlineKeyboardButton("Enable", callback_data="cal:enable:confirm"),
                InlineKeyboardButton("Disable", callback_data="cal:disable:confirm"),
            ],
            [InlineKeyboardButton("Sync now", callback_data="cal:sync:confirm")],
        ],
        "more": [
            [
                InlineKeyboardButton("❔ Help", callback_data="nav:help"),
                InlineKeyboardButton("🏥 Health", callback_data="more:health"),
            ],
            [
                InlineKeyboardButton("📈 Usage", callback_data="more:stats"),
                InlineKeyboardButton("🤖 Models", callback_data="more:models"),
            ],
            [
                InlineKeyboardButton("💾 Backup", callback_data="more:backup:confirm"),
                InlineKeyboardButton("🖥 Server tools", callback_data="more:server"),
            ],
            [InlineKeyboardButton("🛠 Diagnostics", callback_data="more:diagnostics")],
        ],
    }
    return InlineKeyboardMarkup(layouts[section] + navigation_keyboard())


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Daily planning", callback_data="help:daily"),
            InlineKeyboardButton("📚 Study tools", callback_data="help:study"),
        ],
        [
            InlineKeyboardButton("🗓 Calendar", callback_data="help:calendar"),
            InlineKeyboardButton("💬 Assistant input", callback_data="help:input"),
        ],
        [
            InlineKeyboardButton("🛠 System & admin", callback_data="help:admin"),
            InlineKeyboardButton("⌨️ Commands", callback_data="help:commands"),
        ],
    ] + navigation_keyboard())


def confirmation_keyboard(action: str, *, cancel: str = "nav:more") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirm", callback_data=action)],
        [InlineKeyboardButton("Cancel", callback_data=cancel)],
    ])


SECTION_TEXT = {
    "today": (
        "<b>Today</b>\n\n"
        "Refresh your digest to collect the latest Canvas, Classroom, Gmail, and GroupMe updates. "
        "Task cards let you update Notion without typing command arguments."
    ),
    "ask": (
        "<b>Ask anything</b>\n\n"
        "Send a message, reply to a previous response for context, record a voice note, or attach a photo with a question in its caption. "
        "Your existing privacy routing remains active."
    ),
    "study": (
        "<b>Study tools</b>\n\n"
        "Check current coursework, send a completed practice photo for feedback, or ask me to build a guide for a topic."
    ),
    "calendar": (
        "<b>Assignment calendar</b>\n\n"
        "Preview proposed changes before enabling sync. Calendar writes remain disabled until you explicitly enable them."
    ),
    "more": (
        "<b>More tools</b>\n\n"
        "Health, usage, models, backups, diagnostics, and server controls live here so daily planning stays focused."
    ),
}


HELP_TEXT = {
    "daily": "<b>Daily planning</b>\n\nUse <b>Today</b> to refresh your digest and take action on assignment cards. <code>/summary</code> remains available when you prefer commands.",
    "study": "<b>Study tools</b>\n\nUse <b>Study</b> for coursework checks, practice-photo help, and guide creation. You can also send a photo with a question as its caption.",
    "calendar": "<b>Calendar</b>\n\nOpen <b>Calendar</b> to check status, preview assignments, or control sync. <code>/calendar preview</code> is the safe command fallback.",
    "input": "<b>Assistant input</b>\n\nType naturally, reply to a response for context, send a voice note, or send a photo. Use <code>/model</code> to manage the selected model.",
    "admin": "<b>System & admin</b>\n\nHealth, usage, backup, diagnostics, and server controls are under <b>More</b>. State-changing actions ask for confirmation. <code>/bash</code> remains command-only.",
    "commands": "<b>Command reference</b>\n\nCore: <code>/summary</code>, <code>/canvas</code>, <code>/calendar</code>, <code>/model</code>\nSystem: <code>/ping</code>, <code>/stats</code>, <code>/backup</code>, <code>/errors</code>, <code>/server</code>\nAdvanced: <code>/restore</code>, <code>/correlations</code>, <code>/classroom</code>, <code>/bash</code>, <code>/p</code>",
}
