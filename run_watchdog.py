#!/usr/bin/env python3
"""
Watchdog cycle for personal-assistant-bot.
Checks Canvas, Google Classroom, Gmail, GroupMe for new content.
Classifies using Orange Pi 5 classifier (fallback to local Ollama).
Filters NOISE/UNSURE, saves summaries to cache, assembles digest.
Sends Telegram notification if URGENT items found.
"""

import asyncio
import json
import os
import re
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, '/home/sanel/personal-assistant-bot')

from scrapers.canvas_scraper import get_all_canvas_data
from scrapers.composio_fetcher import (
    get_unread_emails,
    get_classroom_assignments, get_classroom_announcements,
    get_recent_google_docs
)
from scrapers.groupme_scraper import get_latest_messages

CACHE_DIR = Path('/home/sanel/personal-assistant-bot/cache')
CACHE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Pi classifier endpoint
PI_CLASSIFIER_URL = "http://10.10.10.2:8080"

async def pi_classify_batch(items: list[dict]) -> list[dict] | None:
    """Send batch to Orange Pi 5 classifier."""
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=60, connect=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{PI_CLASSIFIER_URL}/classify", json=items) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Pi classifier returned HTTP {resp.status}")
                return None
    except Exception as e:
        logger.info(f"Pi classifier unavailable (falling back to local): {e}")
        return None

def local_classify_sync(items: list[dict]) -> list[dict]:
    """Fallback classification using local Ollama qwen2:0.5b."""
    import requests
    
    # For watchdog, just classify the first 10 items max to avoid timeout
    items = items[:10]
    
    prompt = """Classify each item into exactly ONE category.
CRITICAL: Use the EXACT id field from the input (\"0\", \"1\", etc.) in your output.

Categories:
URGENT           - Deadline within 48 hours, emergency alert, teacher direct message
IMPORTANT        - Upcoming deadline (3-14 days), new assignment, test announced
ROUTINE          - Regular club meeting, ongoing assignment, general announcement
GITHUB_CI        - GitHub Actions/CodeQL/lint failure notification
SPAM_PROMO       - College marketing, LinkedIn, generic promotional email
INFORMATIONAL    - Holiday wishes, birthday shoutouts, schedule confirmations
OVERDUE          - Past due date, missed deadline
NOISE            - Unreadable, corrupted, completely irrelevant
UNSURE           - Cannot confidently categorize

Items:
""" + "\n".join([f'{i["id"]}: [{i["source"]}] {i["text"][:300]}' for i in items]) + """

Return ONLY a JSON array: [{"id": "0", "classification": "URGENT", "confidence": 0.95}, ...]"""

    try:
        res = requests.post(
            "http://localhost:11434/api/generate",
            json={"model": "qwen2:0.5b", "prompt": prompt, "stream": False, "options": {"temperature": 0.0}},
            timeout=30
        )
        if res.status_code == 200:
            response_text = res.json().get("response", "").strip()
            # Try to extract JSON - handle multiple formats
            import re
            # Try markdown code block first
            json_match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', response_text, re.DOTALL)
            if not json_match:
                # Try bare array
                json_match = re.search(r'(\[.*\])', response_text, re.DOTALL)
            if json_match:
                try:
                    return json.loads(json_match.group(1))
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        logger.error(f"Local classification failed: {e}")
    
    # Ultimate fallback: everything IMPORTANT
    return [{"id": i["id"], "classification": "IMPORTANT", "confidence": 0.5} for i in items]

async def scrape_sources():
    """Scrape all sources in parallel."""
    results = {}
    
    async def safe_scrape(name, func, *args):
        try:
            return name, await asyncio.to_thread(func, *args)
        except Exception as e:
            logger.error(f"Scraper {name} failed: {e}")
            return name, f"Error: {e}"
    
    tasks = [
        safe_scrape("canvas", get_all_canvas_data),
        safe_scrape("classroom", get_classroom_assignments),
        safe_scrape("classroom_ann", get_classroom_announcements),
        safe_scrape("gmail", get_unread_emails, 5),
        safe_scrape("groupme", get_latest_messages, "102851186"),
        safe_scrape("gdocs", get_recent_google_docs),
    ]
    
    for name, result in await asyncio.gather(*tasks):
        results[name] = result
        # Save raw to cache
        cache_file = CACHE_DIR / f"{name}_raw.txt"
        cache_file.write_text(result or "")
        logger.info(f"Saved {name} raw data ({len(result or '')} chars)")
    
    return results

async def classify_items(raw_data: dict) -> dict:
    """Classify all items from all sources."""
    items = []
    item_id = 0
    
    for source, text in raw_data.items():
        if not text or text.strip() == "" or "Error" in text or "unavailable" in text.lower():
            continue
        
        # Split into individual items (simple heuristic: lines starting with - or [)
        lines = text.split('\n')
        current_item = []
        for line in lines:
            if line.strip().startswith(('-', '[', '📧', '💬', '📚', '🏫', '📢', '📄')) and current_item:
                items.append({"id": str(item_id), "source": source, "text": "\n".join(current_item)[:2000]})
                item_id += 1
                current_item = [line]
            else:
                current_item.append(line)
        if current_item:
            items.append({"id": str(item_id), "source": source, "text": "\n".join(current_item)[:2000]})
            item_id += 1
    
    logger.info(f"Classifying {len(items)} items...")
    
    # Try Pi classifier first
    pi_results = await pi_classify_batch(items)
    
    if pi_results:
        return {r["id"]: r for r in pi_results}
    
    # Fallback to local
    local_results = local_classify_sync(items)
    return {r["id"]: r for r in local_results}


async def classify_items_with_ids(raw_data: dict) -> tuple[dict, dict]:
    """Classify all items from all sources, returning both classifications and item_id_map.
    Returns: (classifications_dict, item_id_map_dict) where item_id_map maps (source, local_index) -> global_id
    """
    items = []
    item_id = 0
    item_id_map = {}  # (source, local_index) -> global_id
    
    for source, text in raw_data.items():
        if not text or text.strip() == "" or "Error" in text or "unavailable" in text.lower():
            continue
        
        # Split into individual items (simple heuristic: lines starting with - or [)
        lines = text.split('\n')
        current_item = []
        local_index = 0
        for line in lines:
            # Skip section header lines (emoji + **...** format)
            if re.match(r'^[\U0001F000-\U0001FFFF]\s*\*\*.*\*\*:?\s*$', line.strip()):
                continue
            if line.strip().startswith(('-', '[', '📧', '💬', '📚', '🏫', '📢', '📄')) and current_item:
                items.append({"id": str(item_id), "source": source, "text": "\n".join(current_item)[:2000]})
                item_id_map[(source, local_index)] = str(item_id)
                item_id += 1
                local_index += 1
                current_item = [line]
            else:
                current_item.append(line)
        if current_item:
            items.append({"id": str(item_id), "source": source, "text": "\n".join(current_item)[:2000]})
            item_id_map[(source, local_index)] = str(item_id)
            item_id += 1
            local_index += 1
    
    logger.info(f"Classifying {len(items)} items...")
    
    # Try Pi classifier first
    pi_results = await pi_classify_batch(items)
    
    if pi_results:
        return {r["id"]: r for r in pi_results}, item_id_map
    
    # Fallback to local
    local_results = local_classify_sync(items)
    return {r["id"]: r for r in local_results}, item_id_map

def filter_and_summarize(raw_data: dict, classifications: dict, item_id_map: dict) -> dict:
    """Filter out NOISE/UNSURE, create summaries.
    Uses item_id_map: (source, local_index) -> global_classification_id
    """
    summaries = {}
    urgent_items = []
    
    for source, text in raw_data.items():
        if not text or text.strip() == "" or "Error" in text or "unavailable" in text.lower():
            summaries[source] = f"No urgent {source} updates."
            continue
        
        lines = text.split('\n')
        current_item = []
        item_summaries = []
        local_index = 0
        
        for line in lines:
            # Skip section header lines (emoji + **...** format)
            if re.match(r'^[\U0001F000-\U0001FFFF]\s*\*\*.*\*\*:?\s*$', line.strip()):
                continue
            if line.strip().startswith(('-', '[', '📧', '💬', '📚', '🏫', '📢', '📄')) and current_item:
                item_text = "\n".join(current_item)
                global_id = item_id_map.get((source, local_index))
                if global_id:
                    cls = classifications.get(global_id, {})
                else:
                    cls = {}
                classification = cls.get("classification", "UNSURE")
                confidence = cls.get("confidence", 0.0)
                
                if classification not in ("NOISE",) and (classification != "UNSURE" or confidence >= 0.7):
                    item_summaries.append(f"{item_text[:200]}")
                    if classification == "URGENT":
                        urgent_items.append(f"[{source}] {item_text[:200]}")
                local_index += 1
                current_item = [line]
            else:
                current_item.append(line)
        
        if current_item:
            item_text = "\n".join(current_item)
            global_id = item_id_map.get((source, local_index))
            if global_id:
                cls = classifications.get(global_id, {})
            else:
                cls = {}
            classification = cls.get("classification", "UNSURE")
            confidence = cls.get("confidence", 0.0)
            
            if classification not in ("NOISE",) and (classification != "UNSURE" or confidence >= 0.7):
                item_summaries.append(f"{item_text[:200]}")
                if classification == "URGENT":
                    urgent_items.append(f"[{source}] {item_text[:200]}")
        
        if item_summaries:
            summaries[source] = "\n".join(item_summaries)
        else:
            summaries[source] = f"No urgent {source} updates."
    
    return {"summaries": summaries, "urgent_items": urgent_items}

async def assemble_digest(summaries: dict) -> str:
    """Assemble final digest using local Llama3.2:3B."""
    import requests
    
    summary_text = "\n\n".join([f"=== {k.upper()} ===\n{v}" for k, v in summaries.items()])
    
    # Truncate if too long
    if len(summary_text) > 8000:
        summary_text = summary_text[:8000] + "\n...[truncated]"
    
    prompt = f"""You are Sanel's personal assistant. Assemble the following classified summaries into a concise Telegram digest.

Rules:
- Use emoji section headers: 📚 Canvas, 🏫 Google Classroom, 📢 Classroom Announcements, 📧 Gmail, 💬 GroupMe
- Keep each section concise. Skip sections that say 'No urgent updates'.
- End with a friendly one-liner.
- Return ONLY the Markdown text.

Summaries:
{summary_text}"""

    # Route through the unified local-inference chain (Surface llama-server
    # at 10.0.0.47:8080, then Pi Ollama) instead of the old hardcoded
    # localhost:11434 model that was never pulled here.
    try:
        from llm_router import call_local_rpc
        result = call_local_rpc(
            prompt=prompt,
            max_tokens=1024,
            temperature=0.3,
            timeout=120,
            allow_cloud=False,
        )
        if result and result.strip():
            return result.strip()
        logger.warning("Digest assembly: local inference unavailable (Surface + Pi), using plain concatenation")
    except Exception as e:
        logger.error(f"Digest assembly failed: {e}")

    # Fallback: simple concatenation
    return "\n\n".join([f"{k}: {v}" for k, v in summaries.items() if "No urgent" not in v])

async def send_telegram(message: str):
    """Send Telegram message."""
    import os
    from dotenv import load_dotenv
    import requests
    
    load_dotenv('/home/sanel/personal-assistant-bot/.env')
    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID')
    
    if not token or not chat_id:
        logger.error("Telegram credentials not configured")
        return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        # Sanitize markdown-breaking chars to avoid Telegram parse errors
        from utils import sanitize_markdown

        # Send in chunks if too long
        for i in range(0, len(message), 4000):
            chunk = message[i:i+4000]
            # Only try Markdown mode with sanitized text
            safe_chunk = sanitize_markdown(chunk)
            res = requests.post(url, json={"chat_id": chat_id, "text": safe_chunk, "parse_mode": "Markdown"}, timeout=30)
            if res.status_code != 200:
                # Retry without markdown (unsanitized — plain text is safe)
                requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=30)
        logger.info("Telegram notification sent")
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")

async def main():
    logger.info("=== Watchdog cycle started ===")
    
    # 1. Scrape all sources
    raw_data = await scrape_sources()
    
    # 2. Classify (with ID mapping)
    classifications, item_id_map = await classify_items_with_ids(raw_data)
    
    # 3. Filter and summarize
    result = filter_and_summarize(raw_data, classifications, item_id_map)
    summaries = result["summaries"]
    urgent_items = result["urgent_items"]
    
    # 4. Save summaries to cache
    for source, summary in summaries.items():
        cache_file = CACHE_DIR / f"{source}_summary.txt"
        cache_file.write_text(summary)
        logger.info(f"Saved {source} summary ({len(summary)} chars)")
    
    # Save combined summaries
    combined_path = CACHE_DIR / "combined_summaries.txt"
    with open(combined_path, "a") as f:
        f.write(f"\n--- Watchdog Cycle {datetime.now(timezone.utc).isoformat()} ---\n")
        for source, summary in summaries.items():
            f.write(f"=== {source.upper()} ===\n{summary}\n\n")
    
    # Save watchdog result
    watchdog_result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "urgent_items": urgent_items,
        "has_urgent": len(urgent_items) > 0
    }
    (CACHE_DIR / "watchdog_result.json").write_text(json.dumps(watchdog_result, indent=2))
    
    # 5. Assemble digest
    digest = await assemble_digest(summaries)
    (CACHE_DIR / "latest_digest.txt").write_text(digest)
    
    # 6. Send Telegram if urgent
    if urgent_items:
        urgent_msg = "🚨 **WATCHDOG ALERT** 🚨\n\n" + "\n\n".join(urgent_items)
        await send_telegram(urgent_msg)
        logger.warning(f"URGENT items found: {len(urgent_items)}")
    else:
        # Send digest anyway (periodic)
        await send_telegram(f"📋 **Periodic Digest**\n\n{digest}")
        logger.info("No urgent items, sent periodic digest")
    
    logger.info("=== Watchdog cycle complete ===")

if __name__ == "__main__":
    asyncio.run(main())
