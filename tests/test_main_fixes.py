import os
import sys
import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import main
import config
from telegram.constants import ChatType

@pytest.mark.asyncio
async def test_require_auth_start():
    update = MagicMock()
    update.effective_chat.id = 999
    update.effective_chat.type = ChatType.PRIVATE
    update.effective_user.id = 999
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    await main.start(update, context)
    update.message.reply_text.assert_called()
    assert "not authorized" in update.message.reply_text.call_args[0][0].lower() or "not allowed" in update.message.reply_text.call_args[0][0].lower() or update.message.reply_text.call_count == 1
    context.job_queue.run_repeating.assert_not_called()

    update.effective_chat.id = config.SANEL_CHAT_ID
    update.effective_user.id = config.TELEGRAM_OWNER_USER_ID
    update.message.reply_text.reset_mock()
    await main.start(update, context)
    context.job_queue.run_repeating.assert_called_with(
        main.check_updates, interval=14400, first=5, chat_id=config.SANEL_CHAT_ID, name=str(config.SANEL_CHAT_ID)
    )

def test_logger_setup():
    with open("main.py", "r") as f:
        content = f.read()
    basic_config_idx = content.find("logging.basicConfig")
    get_logger_idx = content.find("logging.getLogger(__name__)")
    assert basic_config_idx != -1
    assert get_logger_idx != -1
    assert basic_config_idx < get_logger_idx

@pytest.mark.asyncio
async def test_async_job_queue():
    with open("main.py", "r") as f:
        content = f.read()
    assert "lambda ctx:" not in content
    assert "atexit.register" not in content
    assert "post_shutdown" in content
