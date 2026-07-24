import os
import glob
import logging
import asyncio
import httpx
from activity_log import log_nightly

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as source_file:
        return source_file.read()


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as destination_file:
        destination_file.write(text)


async def consolidate_memory():
    import subprocess
    logger.info("Starting 1 AM Pipeline: Preparing system for heavy local AI processing...")
    try:
        # Start Ollama
        # Start Ollama (background process, not systemd)
        import subprocess as _sp
        _sp.Popen(['ollama', 'serve'], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        await asyncio.sleep(5)
    except Exception as e:
        logger.error(f"Failed to prepare system: {e}")

    # Bootstrap: if the embedding index was lost (fresh clone, wiped
    # embedding_data/, etc.), rebuild it from scratch so semantic retrieval
    # isn't disabled until Phase 5's nightly incremental update. Cheap no-op
    # if the npz is already on disk. Non-fatal: Phase 5 already handles rebuild
    # failures and re-tries, so a bootstrap miss just means a degraded
    # semantic-retrieval experience for one cycle.
    try:
        from scrapers.embedding_indexer import rebuild_index_if_missing
        await rebuild_index_if_missing()
    except Exception as e:
        logger.warning(f"Embedding index bootstrap failed (non-critical): {e}")

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from config import CACHE_DIR

    # Gather raw text
    raw_text = ""

    # 1. Read combined_summaries.txt
    summaries_file = os.path.join(CACHE_DIR, "combined_summaries.txt")
    if os.path.exists(summaries_file):
        summaries = await asyncio.to_thread(_read_text, summaries_file)
        raw_text += "\n--- DAILY SUMMARIES AND NOTES ---\n" + summaries

    # 2. Read chat_history files
    chat_files = await asyncio.to_thread(glob.glob, os.path.join(base_dir, "chat_history_*.txt"))
    for cf in chat_files:
        history = await asyncio.to_thread(_read_text, cf)
        raw_text += f"\n--- CHAT HISTORY ({os.path.basename(cf)}) ---\n" + history

    if not raw_text.strip():
        logger.info("No raw memory to consolidate tonight.")
        return

    logger.info("Consolidating memory via Llama 3 8B...")
    prompt = (
        "You are the central Memory Consolidation Engine. It is 2:00 AM. Your task is to process the following raw, messy logs from the day "
        "(including scraped assignments, chat history, and auto-transcribed handwritten notes) and compress them into a pristine, beautifully organized 'curated brain' document.\n\n"
        "Format your output in Markdown with the following sections:\n"
        "- **Upcoming Deadlines & Tasks**\n"
        "- **Current Study Topics** (What is the user currently learning based on their notes? Be specific.)\n"
        "- **Key Insights** (Important things to remember, group drama, or overarching themes).\n\n"
        "Discard all redundant greetings, boilerplate text, and irrelevant chatter. Be concise.\n\n"
        f"RAW DATA:\n{raw_text[:40000]}"
    )

    # Route through the unified local-inference chain (Surface llama-server
    # at 10.0.0.47:8080, then Pi Ollama). PII stays on-cluster. Falls back to
    # secure local G1 Flash (agy) only if the whole local chain is down.
    from llm_router import call_local_rpc
    try:
        brain = await asyncio.to_thread(
            call_local_rpc,
            prompt=prompt,
            max_tokens=2048,
            temperature=0.2,
            timeout=300,
            classification="PRIVATE",
        )
        if brain:
            brain = brain.strip()
        if not brain:
            raise Exception("Local inference chain (Surface + Pi) returned empty")
    except Exception as e:
        logger.warning(f"Local RPC failed to consolidate memory ({type(e).__name__}: {e}). Falling back to secure local G1 Flash to protect PII...")
        from ai_processor import call_agy
        brain = await asyncio.to_thread(call_agy, prompt, timeout=180, model="flash")

    if not brain:
        logger.error("All local models failed to consolidate memory. Aborting to protect PII.")
        return

    # Phase 1: Write brain to file
    try:
        brain_file = os.path.join(base_dir, "curated_brain.md")
        existing_brain = ""
        if os.path.exists(brain_file):
            existing_brain = await asyncio.to_thread(_read_text, brain_file)

        final_brain = brain
        if existing_brain:
            # Merge old and new
            merge_prompt = f"Merge the old brain and new daily insights into a single cohesive document.\n\nOLD BRAIN:\n{existing_brain}\n\nNEW INSIGHTS:\n{brain}"
            try:
                merged_out = await asyncio.to_thread(
                    call_local_rpc,
                    prompt=merge_prompt,
                    max_tokens=2048,
                    temperature=0.2,
                    timeout=300,
                    classification="PRIVATE",
                )
                if merged_out and merged_out.strip():
                    final_brain = merged_out.strip()
                else:
                    raise Exception("Local RPC merge returned empty (Surface + Pi down)")
            except Exception as e:
                logger.warning(f"Local RPC failed to merge brain ({e}). Falling back to secure local G1 Flash...")
                from ai_processor import call_agy
                merged = await asyncio.to_thread(call_agy, merge_prompt, timeout=180, model="flash")
                if merged: final_brain = merged

        await asyncio.to_thread(_write_text, brain_file, final_brain)
    except Exception as e:
        logger.error(f"Failed to write brain file: {e}")

    # Phase 2: Trigger Deep-Dive Online Researcher
    logger.info("Triggering offline topic researcher...")
    try:
        import subprocess
        await asyncio.to_thread(
            subprocess.run,
            ["python3", os.path.join(base_dir, "overnight_researcher.py")],
            timeout=1800,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Overnight researcher timed out — continuing")
    except Exception as e:
        logger.warning(f"Overnight researcher failed (non-critical): {e}")

    # Phase 3: Massive Historical Indexing
    logger.info("Running massive historical data export...")
    try:
        from scrapers.historical_export import run_all_exports
        await asyncio.to_thread(run_all_exports)
    except Exception as e:
        logger.warning(f"Historical export failed (non-critical): {e}")

    # Phase 3.5: Download Classroom PDFs
    logger.info("Downloading Classroom PDFs...")
    try:
        from scrapers.google_scraper import download_classroom_pdfs
        pdf_result = await asyncio.to_thread(download_classroom_pdfs, "classroom_pdfs")
        logger.info(f"Classroom PDF download: {pdf_result}")
        log_nightly("classroom_pdfs", "completed")
    except Exception as e:
        logger.warning(f"Classroom PDF download failed (non-critical): {e}")
        log_nightly("classroom_pdfs", "failed", {"error_type": type(e).__name__})

    # Phase 4: Nightly Indexer
    logger.info("Running nightly massive indexer via OpenRouter...")
    try:
        from scrapers.offline_indexer import run_indexing
        await run_indexing()
    except Exception as e:
        logger.error(f"Nightly indexer failed: {e}")

    # Phase 5: Embedding Index Rebuild (while Ollama is still awake)
    logger.info("Running embedding index rebuild via Ollama nomic-embed-text...")
    try:
        from scrapers.embedding_indexer import build_index
        log_nightly("embedding_indexer", "started")
        success = await build_index()
        if success:
            logger.info("Embedding index rebuilt successfully.")
            log_nightly("embedding_indexer", "completed")
            # Invalidate the semantic retrieval cache so next query picks up new index
            try:
                from scrapers.semantic_retrieval import invalidate_cache
                invalidate_cache()
            except Exception:
                pass
        else:
            logger.warning("Embedding index rebuild failed (non-critical).")
            log_nightly("embedding_indexer", "failed")
    except Exception as e:
        logger.warning(f"Embedding index rebuild failed (non-critical): {e}")
        log_nightly("embedding_indexer", "error", {"error_type": type(e).__name__})

    logger.info("Daily pipeline complete.")

    # Phase 6: Trim raw logs (respect rotation limits, don't fully wipe)
    try:
        # Trim combined_summaries instead of full delete
        if os.path.exists(summaries_file):
            size = await asyncio.to_thread(os.path.getsize, summaries_file)
            if size > 90000:  # keep last ~50%, written by rotation
                content = await asyncio.to_thread(_read_text, summaries_file)
                # Keep only last half
                mid = len(content) // 2
                await asyncio.to_thread(_write_text, summaries_file, "[older entries trimmed]\n" + content[mid:])
                logger.info(f"Trimmed combined_summaries from {size} to ~{size//2} bytes")
        # Don't delete chat files — they have conversation history the user might reference
        logger.info("Memory consolidation complete!")
    except Exception as e:
        logger.error(f"Failed to trim raw logs: {e}")

if __name__ == "__main__":
    asyncio.run(consolidate_memory())
