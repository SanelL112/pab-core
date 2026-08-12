import requests
import asyncio
from telegram import Bot
import logging
import config
logger = logging.getLogger(__name__)

NOTION_API_KEY = config.NOTION_API_KEY
DATABASE_ID = config.NOTION_DATABASE_ID
TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
SANEL_CHAT_ID = config.SANEL_CHAT_ID

def get_pending_tasks():
    """Fetch ALL pending Notion tasks with pagination (not just first 100)."""
    if not NOTION_API_KEY: return []
    query_url = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    payload = {
        "page_size": 100,
        "filter": {
            "property": "Status",
            "status": {"equals": "Not started"}
        }
    }
    try:
        all_tasks = []
        has_more = True
        start_cursor = None
        while has_more:
            p = dict(payload)
            if start_cursor:
                p["start_cursor"] = start_cursor
            res = requests.post(query_url, headers=headers, json=p, timeout=10)
            res.raise_for_status()
            data = res.json()
            results = data.get("results", [])
            for r in results:
                title_props = r.get("properties", {}).get("Task name", {}).get("title", [])
                title = title_props[0].get("text", {}).get("content", "Unknown") if title_props else "Unknown"
                all_tasks.append(title)
            has_more = data.get("has_more", False)
            start_cursor = data.get("next_cursor")
        logger.info(f"Fetched {len(all_tasks)} pending Notion tasks (with pagination)")
        return all_tasks
    except Exception as e:
        logger.error(f"Failed to fetch pending tasks: {e}")
        return []

async def send_morning_digest():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    context_file = config.BOT_CONTEXT_FILE
    
    bot_context = ""
    if context_file.is_file() and not context_file.is_symlink():
        with context_file.open("r", encoding="utf-8") as f:
            bot_context = f.read().strip()
            
    tasks = await asyncio.to_thread(get_pending_tasks)
    
    msg = "☀️ **GOOD MORNING! Here is your Daily Digest.**\n\n"
    
    if bot_context:
        msg += f"🧠 **Current Brain Context:**\n{bot_context}\n\n"
        
    if tasks:
        msg += "📋 **Pending Notion Tasks:**\n"
        for t in tasks:
            msg += f"- {t}\n"
        msg += "\n_Reply to me with updates on your progress for any of these tasks!_"
    else:
        msg += "📋 **Pending Tasks:** You have no pending tasks in Notion! Great job."
        
    # Telegram messages max out at 4096 chars; split if needed
    max_len = 4096
    for i in range(0, len(msg), max_len):
        chunk = msg[i:i+max_len]
        try:
            await bot.send_message(chat_id=SANEL_CHAT_ID, text=chunk, parse_mode="Markdown")
        except Exception:
            try:
                await bot.send_message(chat_id=SANEL_CHAT_ID, text=chunk)
            except Exception as e:
                logger.error(f"Failed to send digest chunk: {e}")
    logger.info("Morning digest sent successfully.")

if __name__ == "__main__":
    asyncio.run(send_morning_digest())
