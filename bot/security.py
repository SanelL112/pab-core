"""Authorization checks for Telegram's external handler boundary."""
from __future__ import annotations

import logging
from functools import wraps

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes

import config

logger = logging.getLogger(__name__)

def _authorized(update: Update | None) -> tuple[bool, int | None, int | None]:
    """Return the policy decision plus non-secret IDs for diagnostics."""
    if update is None:
        return False, None, None
    chat = update.effective_chat
    user = update.effective_user
    chat_id = getattr(chat, "id", None)
    user_id = getattr(user, "id", None)
    chat_type = getattr(chat, "type", None)
    allowed = (
        chat_id is not None
        and user_id is not None
        and chat_type == ChatType.PRIVATE
        and int(chat_id) == int(config.TELEGRAM_CHAT_ID)
        and int(user_id) == int(config.TELEGRAM_OWNER_USER_ID)
    )
    return allowed, chat_id, user_id


def require_auth(func):
    """Allow only the configured owner in the configured private chat."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        allowed, chat_id, user_id = _authorized(update)
        if not allowed:
            logger.warning(
                "Denied Telegram handler invocation (chat_id=%s, user_id=%s)",
                chat_id,
                user_id,
            )
            message = getattr(update, "message", None) if update else None
            query = getattr(update, "callback_query", None) if update else None
            if message:
                await message.reply_text("⛔ This bot is private.")
            elif query:
                await query.answer("⛔ This bot is private.", show_alert=True)
            return None

        return await func(update, context, *args, **kwargs)

    return wrapper
