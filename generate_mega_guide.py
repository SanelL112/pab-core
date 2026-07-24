#!/usr/bin/env python3
"""
Mega Study Guide Generator — Runs as a scheduled cron job at 2 AM daily.
1. Uses the rebuilt embedding index from the 1 AM job
2. Fetches all cached summaries from /home/sanel/personal-assistant-bot/cache/
3. Uses the embedding index to find relevant content across all sources
4. Generates a comprehensive mega study guide with the local inference path
5. Saves the guide to /home/sanel/personal-assistant-bot/output/mega_guide_YYYY-MM-DD.md
6. Sends a Telegram notification with the guide summary
7. Logs the generation time and any errors
"""

import os
import sys
import json
import logging
import datetime
import traceback
from pathlib import Path

# Add project root to path
BASE_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

import httpx

# Configure logging
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / f"mega_guide_{datetime.datetime.now().strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Import local config and modules
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    CONVERSATION_ID,
    BASE_DIR,
    CACHE_DIR,
    ARCHIVE_DIR,
    MEGA_INDEX_FILE,
    MEGA_GUIDE_CLOUD_CLASSIFICATION,
    OPENROUTER_API_KEY,
    OR_DEFAULT_MODEL,
    OR_FALLBACK_MODEL,
    OR_THIRD_MODEL,
)
from ai_processor import call_agy
from scrapers.semantic_retrieval import semantic_search


# ── Constants ────────────────────────────────────────────────────────────────
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

TODAY = datetime.datetime.now().strftime("%Y-%m-%d")
OUTPUT_FILE = OUTPUT_DIR / f"mega_guide_{TODAY}.md"
CHAT_ID = str(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else str(CONVERSATION_ID) or "8534649457"


# ── Helper Functions ──────────────────────────────────────────────────────────

# Define local paths
KNOWLEDGE_BASE_DIR = BASE_DIR / "knowledge_base"
STUDY_GUIDES_DIR = BASE_DIR / "study_guides"
OFFLINE_ARCHIVE_DIR = BASE_DIR / "offline_archive"

def load_cached_summaries() -> dict:
    """Load all cached summary files from the cache directory."""
    summaries = {}
    cache_files = {
        "canvas": "canvas_summary.txt",
        "classroom_assignments": "classroom_summary.txt",
        "classroom_announcements": "classroom_announcements_summary.txt",
        "combined": "combined_summaries.txt",
        "gdocs": "gdocs_summary.txt",
        "gmail": "gmail_summary.txt",
        "groupme": "groupme_summary.txt",
        "latest_digest": "latest_digest.txt",
        "latest_tasks": "latest_tasks.json",
        "latest_topics": "latest_topics.json",
        "watchdog": "watchdog_result.json",
    }
    
    for key, filename in cache_files.items():
        path = CACHE_DIR / filename
        if path.exists():
            try:
                if filename.endswith(".json"):
                    with open(path, "r") as f:
                        summaries[key] = json.load(f)
                else:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        summaries[key] = f.read()
                logger.info(f"Loaded cache: {key} ({len(str(summaries[key]))} chars)")
            except Exception as e:
                logger.warning(f"Failed to load {filename}: {e}")
                summaries[key] = ""
        else:
            logger.warning(f"Cache file not found: {filename}")
            summaries[key] = ""
    
    return summaries


def load_offline_archives() -> dict:
    """Load recent offline archive files."""
    archives = {}
    
    # Get most recent combined summaries
    archive_files = sorted(ARCHIVE_DIR.glob("combined_summaries_*.txt"))
    if archive_files:
        latest = archive_files[-1]
        try:
            with open(latest, "r", encoding="utf-8", errors="replace") as f:
                archives["latest_combined_summaries"] = f.read()
            logger.info(f"Loaded archive: {latest.name} ({len(archives['latest_combined_summaries'])} chars)")
        except Exception as e:
            logger.warning(f"Failed to load {latest}: {e}")
    
    # Get mega index
    if MEGA_INDEX_FILE.exists():
        try:
            with open(MEGA_INDEX_FILE, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
                archives["mega_index"] = content[-50000:]  # Last 50KB
            logger.info(f"Loaded mega_index.md ({len(archives['mega_index'])} chars)")
        except Exception as e:
            logger.warning(f"Failed to load mega_index.md: {e}")
    
    return archives


def load_knowledge_base() -> str:
    """Load knowledge base files."""
    kb_content = []
    if KNOWLEDGE_BASE_DIR.exists():
        for f in sorted(KNOWLEDGE_BASE_DIR.glob("*.md")):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    kb_content.append(f"--- {f.name} ---\n{fh.read()[:15000]}")
            except Exception as e:
                logger.warning(f"Failed to load {f}: {e}")
    
    return "\n\n".join(kb_content)


def load_study_guides() -> str:
    """Load study guide summaries (first 20KB each)."""
    guides = []
    if STUDY_GUIDES_DIR.exists():
        for f in sorted(STUDY_GUIDES_DIR.glob("*.md")):
            try:
                with open(f, "r", encoding="utf-8", errors="replace") as fh:
                    guides.append(f"--- {f.name} ---\n{fh.read()[:20000]}")
            except Exception as e:
                logger.warning(f"Failed to load {f}: {e}")
    
    return "\n\n".join(guides)


def get_semantic_context(topics: list) -> str:
    """Use embedding index to find relevant content for given topics."""
    if not topics:
        return ""
    
    context_parts = []
    for topic in topics:
        try:
            results = semantic_search(topic, top_k=5)
            if results:
                for r in results:
                    source = Path(r["source"]).name
                    score = r["score"] * 100
                    context_parts.append(
                        f"[SEMANTIC: {source} | relevance: {score:.0f}% | topic: {topic}]\n{r['text']}"
                    )
            logger.info(f"Semantic search for '{topic}': {len(results)} results")
        except Exception as e:
            logger.warning(f"Semantic search failed for '{topic}': {e}")
    
    return "\n\n---\n\n".join(context_parts)


def extract_topics_from_cache(summaries: dict) -> list:
    """Extract potential study topics from cached summaries."""
    topics = []
    
    # From latest_topics.json
    if summaries.get("latest_topics"):
        try:
            topics_data = summaries["latest_topics"]
            if isinstance(topics_data, list):
                topics.extend([t.get("topic", "") for t in topics_data if t.get("topic")])
            elif isinstance(topics_data, dict):
                topics.extend(topics_data.get("topics", []))
        except Exception:
            pass
    
    # From tasks
    if summaries.get("latest_tasks"):
        try:
            tasks_data = summaries["latest_tasks"]
            if isinstance(tasks_data, list):
                for task in tasks_data:
                    if isinstance(task, dict):
                        title = task.get("title", "")
                        if title:
                            topics.append(title)
        except Exception:
            pass
    
    # From combined summaries - extract academic terms
    combined = summaries.get("combined", "")
    if combined:
        import re
        matches = re.findall(
            r'\[([^\]]+(?:SAT|ACT|Math|Physics|Chemistry|Biology|History|English|Grammar|Geometry|Algebra|Calculus|Statistics|Writing|Reading)[^\]]*)\]',
            combined
        )
        topics.extend(matches)
    
    # Deduplicate and clean
    unique_topics = []
    seen = set()
    for t in topics:
        clean = t.strip()
        if clean and len(clean) > 3 and clean.lower() not in seen:
            seen.add(clean.lower())
            unique_topics.append(clean)
    
    return unique_topics[:15]  # Limit to top 15


def build_mega_guide_prompt(topics: list, all_context: dict) -> str:
    """Build the comprehensive prompt for the mega study guide."""
    
    context_sections = []
    
    if all_context.get("semantic"):
        context_sections.append(f"=== SEMANTIC RETRIEVAL RESULTS ===\n{all_context['semantic']}")
    
    if all_context.get("cache_combined"):
        context_sections.append(f"=== CACHED SUMMARIES (COMBINED) ===\n{all_context['cache_combined'][:50000]}")
    
    if all_context.get("cache_canvas"):
        context_sections.append(f"=== CANVAS ASSIGNMENTS ===\n{all_context['cache_canvas']}")
    
    if all_context.get("cache_classroom"):
        context_sections.append(f"=== GOOGLE CLASSROOM ===\n{all_context['cache_classroom']}")
    
    if all_context.get("archive_combined"):
        context_sections.append(f"=== ARCHIVED CLASS NOTES ===\n{all_context['archive_combined'][:50000]}")
    
    if all_context.get("knowledge_base"):
        context_sections.append(f"=== KNOWLEDGE BASE ===\n{all_context['knowledge_base'][:30000]}")
    
    if all_context.get("study_guides"):
        context_sections.append(f"=== EXISTING STUDY GUIDES ===\n{all_context['study_guides'][:30000]}")
    
    if all_context.get("mega_index"):
        context_sections.append(f"=== MEGA INDEX (RECENT) ===\n{all_context['mega_index']}")
    
    full_context = "\n\n".join(context_sections)
    
    topic_list = "\n".join(f"- {t}" for t in topics) if topics else "(No specific topics identified - create comprehensive SAT/ACT prep guide)"
    
    prompt = f"""You are an elite academic tutor creating the ULTIMATE MEGA STUDY GUIDE for a high school student preparing for SAT/ACT and advanced coursework.

TARGET TOPICS (detected from recent assignments, notes, and tests):
{topic_list}

You have access to the following comprehensive context gathered from the student's actual classroom materials, assignments, notes, and past study guides:

{full_context}

=== YOUR TASK ===
Create an EXHAUSTIVE, beautifully formatted Markdown study guide that covers ALL of the above topics and their underlying concepts. This guide will be the student's PRIMARY study resource.

REQUIREMENTS:
1. STRUCTURE: Organize by major topic area (SAT Math, SAT Reading, SAT Writing, ACT English, ACT Math, ACT Reading, ACT Science, Class-Specific Topics)
2. DEPTH: For EACH topic, provide:
   - Core formulas/theorems/rules (with derivations)
   - Deep-dive explanations of concepts
   - Step-by-step problem-solving strategies
   - Common traps and how to avoid them
   - Practice problems with detailed solutions
3. INTEGRATION: Connect classroom notes to test concepts explicitly
4. PRIORITIZATION: Mark high-yield topics (⭐) vs. lower-yield (📝)
5. ACTION PLAN: End with a 2-week study schedule
6. FORMAT: Use clear headers, bold key terms, LaTeX math ($x^2$, $$E=mc^2$$), tables, and bullet points

OUTPUT FORMAT (Markdown):
# 🧠 MEGA STUDY GUIDE — {TODAY}

## 📋 Executive Summary
[1-paragraph overview of what this guide covers and how to use it]

## 🎯 Topic Priority Matrix
[Table: Topic | Source | Priority | Estimated Study Hours]

## 📚 SECTION 1: SAT MATH MASTERY
### 1.1 Heart of Algebra
...
### 1.2 Problem Solving & Data Analysis
...
### 1.3 Passport to Advanced Math
...
### 1.4 Additional Topics (Geometry, Trig, Complex Numbers)
...

## 📚 SECTION 2: SAT READING & WRITING
### 2.1 Reading: Information & Ideas
...
### 2.2 Reading: Craft & Structure
...
### 2.3 Writing: Standard English Conventions
...
### 2.4 Writing: Expression of Ideas
...

## 📚 SECTION 3: ACT COMPREHENSIVE
### 3.1 ACT English
...
### 3.2 ACT Math
...
### 3.3 ACT Reading
...
### 3.4 ACT Science
...

## 📚 SECTION 4: CLASS-SPECIFIC DEEP DIVES
[For each detected class topic from the context above]
### 4.x [Topic Name]
[Detailed coverage linking classroom notes to test concepts]

## 🛠️ SECTION 5: MASTER STRATEGIES & TACTICS
### 5.1 Pacing Strategies
### 5.2 Elimination Techniques
### 5.3 Mental Math Shortcuts
### 5.4 Reading Comprehension Frameworks
### 5.5 Grammar Rule Quick-Reference

## 📝 SECTION 6: PRACTICE PROBLEMS & SOLUTIONS
[15-20 challenging problems across all sections with step-by-step solutions]

## 📅 SECTION 7: 14-DAY MASTER STUDY PLAN
[Day-by-day schedule with specific topics, practice sets, and review]

## 📌 SECTION 8: MEMORIZATION CHECKLIST
[Categorized list of every formula, rule, and fact to memorize]

## 📚 SOURCES & REFERENCES
[List of all sources used from the context above]

---
Generate the COMPLETE guide now. Be exhaustive. No token limits - write until done.
"""
    return prompt


def call_guide_model(prompt: str, timeout: int = 600) -> str:
    """Generate a guide with an explicit cloud opt-in and local fallback.

    The environment must set ``MEGA_GUIDE_CLOUD_CLASSIFICATION=PUBLIC`` before
    cached school and personal material can be sent to OpenRouter.  Otherwise,
    this path remains local-only.
    """
    cloud_attempted = False
    if MEGA_GUIDE_CLOUD_CLASSIFICATION == "PUBLIC" and OPENROUTER_API_KEY:
        cloud_attempted = True
        from llm_router import call_openrouter

        result = call_openrouter(
            model=OR_DEFAULT_MODEL,
            prompt=prompt,
            task="mega-guide",
            max_tokens=8000,
            system_prompt=(
                "You are an elite academic tutor creating comprehensive study "
                "guides. Be exhaustive, precise, and well-structured."
            ),
            fallback_chain=[OR_FALLBACK_MODEL, OR_THIRD_MODEL],
            timeout=timeout,
            classification="PUBLIC",
        )
        if result and not result.startswith("⚠️"):
            logger.info("Cloud mega-guide inference succeeded: %s chars", len(result))
            return result
        logger.warning("Cloud mega-guide inference failed; falling back to local inference")

    try:
        result = call_agy(prompt, timeout=timeout, model="flash")
    except Exception as exc:
        logger.warning("Local mega-guide inference failed: %s", type(exc).__name__)
        result = ""

    if result:
        logger.info("Local mega-guide inference succeeded: %s chars", len(result))
        return result
    if cloud_attempted:
        return "❌ Local inference unavailable after approved cloud generation failed."
    return "❌ Local inference unavailable; private mega-guide content was not sent to cloud."


def send_telegram_message(text: str, parse_mode: str = None) -> bool:
    """Send a message via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("No TELEGRAM_BOT_TOKEN configured")
        return False
    
    # Sanitize markdown-breaking chars from dynamic content
    from utils import sanitize_markdown
    safe_text = sanitize_markdown(text) if parse_mode == "Markdown" else text
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    
    # Split long messages
    max_len = 4000
    for i in range(0, len(safe_text), max_len):
        chunk = safe_text[i:i+max_len]
        payload = {
            "chat_id": CHAT_ID,
            "text": chunk,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        
        try:
            with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
                resp = client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.error(f"Telegram send failed: {resp.status_code} - {resp.text}")
                    return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False
    
    return True


def send_telegram_document(file_path: Path, caption: str = "") -> bool:
    """Send a document via Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0)) as client:
            with open(file_path, "rb") as f:
                files = {"document": (file_path.name, f, "text/markdown")}
                data = {"chat_id": CHAT_ID, "caption": caption[:1000]}
                resp = client.post(url, data=data, files=files)
                if resp.status_code == 200:
                    logger.info(f"Sent document: {file_path.name}")
                    return True
                else:
                    logger.error(f"Telegram document send failed: {resp.status_code} - {resp.text}")
                    return False
    except Exception as e:
        logger.error(f"Telegram document error: {e}")
        return False


def main():
    """Main entry point for mega guide generation."""
    start_time = datetime.datetime.now()
    logger.info(f"=== MEGA STUDY GUIDE GENERATION STARTED at {start_time} ===")
    
    try:
        # 1. Load all cached summaries
        logger.info("Loading cached summaries...")
        summaries = load_cached_summaries()
        
        # 2. Load offline archives
        logger.info("Loading offline archives...")
        archives = load_offline_archives()
        
        # 3. Load knowledge base and study guides
        logger.info("Loading knowledge base and study guides...")
        kb_content = load_knowledge_base()
        sg_content = load_study_guides()
        
        # 4. Extract topics from cache
        logger.info("Extracting topics from cache...")
        topics = extract_topics_from_cache(summaries)
        logger.info(f"Extracted topics: {topics}")
        
        # 5. Get semantic context from embedding index
        logger.info("Querying embedding index for semantic context...")
        semantic_context = get_semantic_context(topics)
        
        # 6. Build all context
        all_context = {
            "semantic": semantic_context,
            "cache_combined": summaries.get("combined", ""),
            "cache_canvas": summaries.get("canvas", ""),
            "cache_classroom": summaries.get("classroom_assignments", ""),
            "archive_combined": archives.get("latest_combined_summaries", ""),
            "knowledge_base": kb_content,
            "study_guides": sg_content,
            "mega_index": archives.get("mega_index", ""),
        }
        
        # 7. Build prompt
        logger.info("Building mega guide prompt...")
        prompt = build_mega_guide_prompt(topics, all_context)
        logger.info(f"Prompt length: {len(prompt)} chars")
        
        # 8. Cloud generation requires an explicit deployment opt-in.
        logger.info("Generating mega study guide through the configured inference path...")
        guide_content = call_guide_model(prompt)
        
        if guide_content.startswith("❌"):
            raise RuntimeError(f"All models failed: {guide_content}")
        
        # 9. Save to file
        logger.info(f"Saving guide to {OUTPUT_FILE}...")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(guide_content)
        
        file_size = OUTPUT_FILE.stat().st_size
        logger.info(f"Guide saved: {file_size:,} bytes ({file_size/1024:.1f} KB)")
        
        # 10. Send Telegram notification
        logger.info("Sending Telegram notification...")
        
        # Create summary
        word_count = len(guide_content.split())
        line_count = len(guide_content.splitlines())
        
        summary = (
            f"🧠 **MEGA STUDY GUIDE GENERATED** — {TODAY}\n\n"
            f"📊 **Stats:**\n"
            f"• Topics covered: {len(topics)}\n"
            f"• File size: {file_size/1024:.1f} KB\n"
            f"• Word count: ~{word_count:,}\n"
            f"• Lines: {line_count:,}\n"
            f"• Generation time: {(datetime.datetime.now() - start_time).total_seconds():.1f}s\n\n"
            f"📚 **Topics:**\n" + "\n".join(f"• {t}" for t in topics[:10]) + 
            (f"\n• ...and {len(topics)-10} more" if len(topics) > 10 else "") + "\n\n"
            f"📁 Saved to: `output/mega_guide_{TODAY}.md`\n"
            f"🔗 Full guide attached below."
        )
        
        send_telegram_message(summary, parse_mode="Markdown")
        send_telegram_document(OUTPUT_FILE, caption=f"Mega Study Guide — {TODAY}")
        
        # 11. Log completion
        elapsed = (datetime.datetime.now() - start_time).total_seconds()
        logger.info(f"=== MEGA STUDY GUIDE COMPLETED in {elapsed:.1f}s ===")
        
        return True
        
    except Exception as e:
        elapsed = (datetime.datetime.now() - start_time).total_seconds()
        error_msg = f"❌ MEGA GUIDE GENERATION FAILED after {elapsed:.1f}s:\n{type(e).__name__}: {e}\n\n```\n{traceback.format_exc()}\n```"
        logger.error(error_msg)
        
        # Send error notification
        send_telegram_message(f"🚨 **MEGA GUIDE GENERATION FAILED**\n\n{error_msg[:3500]}")
        return False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
