#!/usr/bin/env python3
"""
Daily Digest Generator for Personal Assistant Bot.
Runs at 4 AM daily via Hermes cron job.

1. Fetches all classified items from the last 24 hours from cache
2. Groups items by source (Canvas, Classroom, Gmail, GroupMe)
3. Filters to only URGENT, HOMEWORK, GRADE, ANNOUNCEMENT, MEETING, DELIVERY items
4. Uses local Llama3.2:3B to assemble a concise, well-formatted daily digest in Markdown
5. Saves to /home/sanel/personal-assistant-bot/output/daily_digest_YYYY-MM-DD.md
6. Sends the digest via Telegram
7. Logs generation time and item counts
"""

import os
import sys
import json
import logging
import asyncio
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

# Ensure project root is importable for llm_router (this file lives in scripts/)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
CACHE_DIR = config.CACHE_DIR
OUTPUT_DIR = config.EXPORT_DIR / 'daily_digests'
OLLAMA_URL = config.OLLAMA_LOCAL_URL
CLASSIFY_MODEL = 'qwen2:0.5b'  # Fast model for classification
DIGEST_MODEL = 'hf.co/unsloth/Llama-3.2-3B-Instruct-GGUF:latest'  # Better model for digest assembly
TELEGRAM_BOT_TOKEN = config.TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID = str(config.SANEL_CHAT_ID)

# Filter categories we want in the daily digest
KEEP_CATEGORIES = {'URGENT', 'HOMEWORK', 'GRADE', 'ANNOUNCEMENT', 'MEETING', 'DELIVERY'}

# Category emojis for sections
CATEGORY_EMOJIS = {
    'URGENT': '🚨',
    'HOMEWORK': '📚',
    'GRADE': '📝',
    'ANNOUNCEMENT': '📢',
    'MEETING': '🤝',
    'DELIVERY': '📦'
}

# Section headers for the digest
SECTION_HEADERS = {
    'URGENT': '🚨 Urgent',
    'HOMEWORK': '📚 Homework',
    'GRADE': '📝 Grades',
    'ANNOUNCEMENT': '📢 Announcements',
    'MEETING': '🤝 Meetings',
    'DELIVERY': '📦 Deliveries'
}

SOURCE_EMOJIS = {
    'canvas': '📚',
    'classroom': '🏫',
    'classroom_ann': '📢',
    'gmail': '📧',
    'groupme': '💬',
    'gdocs': '📄'
}


def load_cached_raw_data() -> Dict[str, str]:
    """Load raw cached data from all sources."""
    sources = ['canvas', 'classroom', 'classroom_ann', 'gmail', 'groupme', 'gdocs']
    raw_data = {}

    for source in sources:
        cache_file = CACHE_DIR / f'{source}_raw.txt'
        if cache_file.exists():
            content = cache_file.read_text(encoding='utf-8').strip()
            if content:
                raw_data[source] = content
                logger.info(f"Loaded {source} cache ({len(content)} chars)")
            else:
                raw_data[source] = ""
        else:
            raw_data[source] = ""
            logger.warning(f"Cache file not found: {cache_file}")

    return raw_data


def parse_items_from_raw(source: str, text: str) -> List[Dict[str, Any]]:
    """Parse individual items from raw cached text."""
    if not text or "Error" in text or "unavailable" in text.lower() or "No " in text.split('\n')[0]:
        return []

    items = []
    lines = text.split('\n')
    current_item = []
    item_id = 0

    # Source-specific parsing markers
    if source == 'canvas':
        markers = ('- [', '📚', '📢', '📄')
    elif source == 'classroom':
        markers = ('- [', '📎')
    elif source == 'classroom_ann':
        markers = ('[2026', '[2025', '[2024', '- [', '📢')
    elif source == 'gmail':
        markers = ('From:', 'Subject:', '📧')
    elif source == 'groupme':
        markers = ('💬', '**', 'http', '—')
    elif source == 'gdocs':
        markers = ('--- Doc:', '📄')
    else:
        markers = ('-', '[')

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check if this line starts a new item (but not the very first line)
        is_new_item = False
        if current_item and any(stripped.startswith(m) for m in markers):
            is_new_item = True

        if is_new_item and current_item:
            item_text = '\n'.join(current_item)
            items.append({
                'id': str(item_id),
                'source': source,
                'text': item_text[:2000]  # Truncate to avoid huge items
            })
            item_id += 1
            current_item = [line]
        else:
            current_item.append(line)

    # Don't forget the last item
    if current_item:
        item_text = '\n'.join(current_item)
        items.append({
            'id': str(item_id),
            'source': source,
            'text': item_text[:2000]
        })

    logger.info(f"Parsed {len(items)} items from {source}")
    return items


async def classify_items(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Classify items using local qwen2:0.5b via Ollama (fast)."""
    if not items:
        return {}

    # Process in smaller batches to avoid timeout
    batch_size = 5
    all_classifications = {}

    for i in range(0, len(items), batch_size):
        batch = items[i:i+batch_size]

        # Prepare prompt for classification
        prompt = """You are a classifier. Return ONLY a JSON array of objects with id, classification, and confidence.

Categories: URGENT, HOMEWORK, GRADE, ANNOUNCEMENT, MEETING, DELIVERY, NOISE, UNSURE

URGENT: Deadline within 48 hours, emergency alert, teacher direct message
HOMEWORK: Upcoming assignment, homework, project, test prep (3-14 days out)
GRADE: Grade posted, score returned, gradebook update
ANNOUNCEMENT: General announcement, schedule change, info update
MEETING: Club meeting, class meeting, officer meeting, event
DELIVERY: Package delivery, mail notification, physical item arrival
NOISE: Spam, promotional, irrelevant, corrupted
UNSURE: Cannot confidently categorize

Items:
""" + '\n'.join([f'{item["id"]}: [{item["source"]}] {item["text"][:300]}' for item in batch]) + """

Return JSON array: [{"id": "...", "classification": "...", "confidence": 0.XX}, ...]"""

        try:
            res = await asyncio.to_thread(
                requests.post,
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": CLASSIFY_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.0, "num_predict": 1000},
                },
                timeout=60,
            )

            if res.status_code == 200:
                response_text = res.json().get("response", "").strip()

                # Extract JSON array - handle markdown code blocks
                import re
                json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
                if json_match:
                    classifications = json.loads(json_match.group())
                    for c in classifications:
                        all_classifications[c["id"]] = c
                else:
                    logger.warning(f"Could not extract JSON from classification response: {response_text[:200]}")
            else:
                logger.error(f"Classification failed: HTTP {res.status_code}")
        except Exception as e:
            logger.error(f"Classification error: {e}")

    # Fill in any missing with UNSURE
    for item in items:
        if item["id"] not in all_classifications:
            all_classifications[item["id"]] = {"classification": "UNSURE", "confidence": 0.0}

    logger.info(f"Classified {len(all_classifications)} items")
    return all_classifications


def filter_and_group_items(items: List[Dict[str, Any]], classifications: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, List[str]]]:
    """Filter items by category and group by source and category."""
    grouped = {source: {cat: [] for cat in KEEP_CATEGORIES} for source in SOURCE_EMOJIS.keys()}

    for item in items:
        cls = classifications.get(item["id"], {})
        category = cls.get("classification", "UNSURE")
        confidence = cls.get("confidence", 0.0)

        # Only keep items in our target categories with decent confidence
        if category in KEEP_CATEGORIES and (category != "UNSURE" or confidence >= 0.7):
            source = item["source"]
            if source in grouped:
                grouped[source][category].append(item["text"][:500])

    # Log counts
    total_kept = 0
    for source, cats in grouped.items():
        for cat, items_list in cats.items():
            if items_list:
                total_kept += len(items_list)
                logger.info(f"  {source}/{cat}: {len(items_list)} items")

    logger.info(f"Total items kept for digest: {total_kept}")
    return grouped


def build_digest_prompt(grouped: Dict[str, Dict[str, List[str]]]) -> str:
    """Build the prompt for Llama3.2 to assemble the digest."""
    # Build a structured summary for the LLM
    sections = []

    for source, cats in grouped.items():
        source_items = []
        for cat, items in cats.items():
            if items:
                for item in items:
                    source_items.append(f"  [{cat}] {item[:300]}")

        if source_items:
            sections.append(f"=== {source.upper()} ===\n" + "\n".join(source_items))

    if not sections:
        return ""

    combined = "\n\n".join(sections)

    prompt = f"""You are Sanel's personal assistant. Create a concise daily digest for Telegram from the classified items below.

Rules:
- Use these section headers with emojis: 🚨 Urgent, 📚 Homework, 📝 Grades, 📢 Announcements, 🤝 Meetings, 📦 Deliveries
- Within each section, group by source: 📚 Canvas, 🏫 Classroom, 📧 Gmail, 💬 GroupMe
- Keep each bullet point to ONE line max (truncate long items)
- Skip empty sections entirely
- End with a friendly one-liner
- Return ONLY the Markdown text, no extra commentary

Classified Items:
{combined}"""

    return prompt


def build_fallback_digest(prompt: str) -> str:
    """Build a simple digest manually from the prompt when LLM fails."""
    if not prompt:
        return "☀️ **Daily Digest**\n\nNo items to report today. All caught up! 🎉"
    
    # Parse the prompt to extract items
    import re
    
    # Extract sections from the prompt
    sections_text = prompt.split("Classified Items:\n")[-1] if "Classified Items:" in prompt else prompt
    
    # Group by category
    categories = ['URGENT', 'HOMEWORK', 'GRADE', 'ANNOUNCEMENT', 'MEETING', 'DELIVERY']
    cat_items = {cat: [] for cat in categories}
    
    # Simple parsing: look for [CATEGORY] markers
    for cat in categories:
        pattern = rf'\[{cat}\]\s*(.+?)(?=\n\s*\[|\n===\s|\Z)'
        matches = re.findall(pattern, sections_text, re.DOTALL)
        for match in matches:
            cat_items[cat].append(match.strip()[:200])
    
    # Build digest
    lines = ["☀️ **Daily Digest**\n"]
    
    cat_headers = {
        'URGENT': '🚨 Urgent',
        'HOMEWORK': '📚 Homework',
        'GRADE': '📝 Grades',
        'ANNOUNCEMENT': '📢 Announcements',
        'MEETING': '🤝 Meetings',
        'DELIVERY': '📦 Deliveries'
    }
    
    for cat in categories:
        items = cat_items[cat]
        if items:
            lines.append(f"\n{cat_headers[cat]}")
            for item in items[:5]:  # Limit to 5 per category
                lines.append(f"- {item}")
    
    if len(lines) == 1:
        lines.append("\nNo items to report today. All caught up! 🎉")
    else:
        lines.append("\nHave a great day! 🌟")
    
    return "\n".join(lines)


async def assemble_digest_with_llm(prompt: str) -> str:
    """Assemble the final digest via the unified local-inference chain.

    Routes through call_local_rpc (Surface llama-server at 10.0.0.47:8080,
    then Pi Ollama) instead of the old hardcoded localhost:11434 model that
    was never pulled here.
    """
    if not prompt:
        return "☀️ **Daily Digest**\n\nNo items to report today. All caught up! 🎉"

    try:
        from llm_router import call_local_rpc
        response = await asyncio.to_thread(
            call_local_rpc,
            prompt=prompt,
            max_tokens=2000,
            temperature=0.3,
            timeout=300,
        )
        if response and response.strip():
            response = response.strip()
            logger.info(f"LLM assembled digest ({len(response)} chars)")
            return response

        logger.error("LLM assembly failed: local inference unavailable (Surface + Pi)")
    except Exception as e:
        logger.error(f"LLM assembly error: {e}")

    # Fallback: build digest manually from grouped items
    return build_fallback_digest(prompt)


def save_digest(digest: str) -> Path:
    """Save digest to output directory with date-stamped filename."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime('%Y-%m-%d')
    output_file = OUTPUT_DIR / f'daily_digest_{date_str}.md'
    output_file.write_text(digest, encoding='utf-8')
    logger.info(f"Saved digest to {output_file}")
    return output_file


async def send_telegram_digest(digest: str) -> bool:
    """Send the digest via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not configured")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # Split into chunks if too long
    max_len = 4000
    chunks = [digest[i:i+max_len] for i in range(0, len(digest), max_len)]

    for i, chunk in enumerate(chunks):
        prefix = "☀️ **DAILY DIGEST" + (f" (Part {i+1}/{len(chunks)})" if len(chunks) > 1 else "") + "**\n\n"
        full_msg = prefix + chunk

        try:
            res = await asyncio.to_thread(
                requests.post,
                url,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": full_msg,
                    "parse_mode": "Markdown",
                    "disable_notification": False,
                },
                timeout=30,
            )

            if res.status_code == 200:
                logger.info(f"Telegram chunk {i+1}/{len(chunks)} sent successfully")
            else:
                # Try without markdown
                res2 = await asyncio.to_thread(
                    requests.post,
                    url,
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": full_msg,
                    },
                    timeout=30,
                )
                if res2.status_code == 200:
                    logger.info(f"Telegram chunk {i+1}/{len(chunks)} sent (plain text fallback)")
                else:
                    logger.error(f"Telegram send failed: {res2.status_code} - {res2.text}")
                    return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    return True


async def main():
    config.initialize_runtime()
    """Main daily digest generation pipeline."""
    start_time = datetime.now()
    logger.info("=" * 60)
    logger.info("Starting daily digest generation")
    logger.info("=" * 60)

    # Step 1: Load cached raw data
    logger.info("Step 1: Loading cached data from all sources...")
    raw_data = load_cached_raw_data()

    # Step 2: Parse items from each source
    logger.info("Step 2: Parsing items from raw data...")
    all_items = []
    for source, text in raw_data.items():
        items = parse_items_from_raw(source, text)
        all_items.extend(items)

    logger.info(f"Total items parsed: {len(all_items)}")

    # Step 3: Classify items
    logger.info("Step 3: Classifying items with qwen2:0.5b...")
    classifications = await classify_items(all_items)

    # Step 4: Filter and group
    logger.info("Step 4: Filtering and grouping by category...")
    grouped = filter_and_group_items(all_items, classifications)

    # Step 5: Assemble digest with LLM
    logger.info("Step 5: Assembling digest with Llama3.2:3B...")
    prompt = build_digest_prompt(grouped)
    digest = await assemble_digest_with_llm(prompt)

    # Step 6: Save to file
    logger.info("Step 6: Saving digest to output directory...")
    output_file = save_digest(digest)

    # Step 7: Send via Telegram
    logger.info("Step 7: Sending digest via Telegram...")
    sent = await send_telegram_digest(digest)

    # Logging summary
    elapsed = (datetime.now() - start_time).total_seconds()
    total_items = sum(len(items) for cats in grouped.values() for items in cats.values())

    logger.info("=" * 60)
    logger.info(f"Daily digest generation complete in {elapsed:.1f}s")
    logger.info(f"Items processed: {len(all_items)}")
    logger.info(f"Items in digest: {total_items}")
    logger.info(f"Output file: {output_file}")
    logger.info(f"Telegram sent: {'Yes' if sent else 'No'}")
    logger.info("=" * 60)

    return {
        'success': sent,
        'items_processed': len(all_items),
        'items_in_digest': total_items,
        'output_file': str(output_file),
        'elapsed_seconds': elapsed,
        'digest': digest
    }


if __name__ == "__main__":
    result = asyncio.run(main())
    if not result['success']:
        exit(1)
