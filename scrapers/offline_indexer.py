import os
import json
import asyncio
import httpx
import logging

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(BASE_DIR, "..", "offline_archive")
OUTPUT_FILE = os.path.join(BASE_DIR, "..", "mega_index.md")

def chunk_text(text, chunk_size=8000):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]

async def process_chunk(chunk, chunk_index, source_name, max_retries=2):
    prompt = (
        "You are a highly meticulous academic data curator. Read the following chunk of raw data extracted from the student's learning platforms.\n"
        "You MUST provide an EXHAUSTIVE index of EVERY SINGLE file, document, announcement, and assignment found in this chunk. DO NOT ignore or skip anything, regardless of whether you think it is important or not.\n"
        "For each item, extract and output the following points clearly:\n"
        "1. EXACT TITLE/NAME of the file or item.\n"
        "2. What information is contained in it.\n"
        "3. How this information could be useful for study guides.\n"
        "4. What this reveals about what the student is doing or learning.\n\n"
        f"SOURCE: {source_name}\n"
        f"DATA:\n{chunk}"
    )
    
    # Route through the unified local-inference chain (Surface llama-server
    # at 10.0.0.47:8080, then Pi Ollama). This replaces the old hardcoded
    # localhost:11434 call that required a model that was never pulled here.
    from llm_router import call_local_rpc

    for attempt in range(max_retries):
        try:
            result = await asyncio.to_thread(
                call_local_rpc,
                prompt=prompt,
                max_tokens=2048,
                temperature=0.1,
                timeout=120,
                classification="PRIVATE",
            )
            if result and result.strip():
                return result
            # Empty means all local paths (Surface + Pi) are down — don't retry.
            logger.warning("Local inference unavailable. Skipping offline indexing.")
            return None
        except Exception as e:
            logger.error(f"Error during local inference: {e}. Retrying ({attempt+1}/{max_retries})...")
            await asyncio.sleep(5)

    logger.warning(f"Local indexing failed after {max_retries} attempts")
    return None

import hashlib

PROGRESS_FILE = os.path.join(ARCHIVE_DIR, ".delta_index_progress.json")

def _load_progress(delta_hash):
    """Return the number of chunks already indexed for THIS exact delta content.
    If the delta file changed (new hash), progress resets to 0 so we don't skip
    freshly-appended data."""
    try:
        with open(PROGRESS_FILE) as f:
            p = json.load(f)
        if p.get("delta_hash") == delta_hash:
            return int(p.get("done", 0))
    except Exception:
        pass
    return 0

def _save_progress(delta_hash, done):
    try:
        with open(PROGRESS_FILE, "w") as f:
            json.dump({"delta_hash": delta_hash, "done": done}, f)
    except Exception as e:
        logger.error(f"Failed to persist index progress: {e}")

async def run_indexing():
    logger.info("Starting Massive Historical Indexing...")
    delta_file = os.path.join(ARCHIVE_DIR, "delta_export.txt")
    if not os.path.exists(delta_file):
        logger.info("No delta file found. Nothing to index.")
        return
        
    try:
        with open(delta_file, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except Exception as e:
        logger.error(f"Failed to read delta_export.txt: {e}")
        return
        
    if not text.strip():
        logger.info("Delta file empty.")
        return
        
    chunks = list(chunk_text(text, chunk_size=8000))
    total_chunks = len(chunks)

    # Resume from where a previous partial run left off (keyed to the delta
    # content hash) so we don't re-index — and DUPLICATE in mega_index.md —
    # chunks that already succeeded. A changed delta file resets the cursor.
    delta_hash = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    already_done = _load_progress(delta_hash)
    if already_done >= total_chunks:
        # Everything already indexed on a prior run; clear delta + progress.
        open(delta_file, 'w').close()
        _save_progress(delta_hash, 0)
        logger.info("Delta already fully indexed — cleared. Nothing to do.")
        return
    if already_done > 0:
        logger.info(f"Resuming delta indexing at chunk {already_done+1}/{total_chunks} "
                    f"({already_done} already indexed).")

    success_count = already_done
    local_inference_down = False
    for i in range(already_done, total_chunks):
        chunk = chunks[i]
        logger.info(f"Processing chunk {i+1}/{total_chunks} for delta_export...")
        result = await process_chunk(chunk, i+1, "delta_export")
        if result:
            with open(OUTPUT_FILE, "a", encoding="utf-8") as out_f:
                out_f.write(f"\n\n## Source: Nightly Delta (Part {i+1}/{total_chunks})\n\n")
                out_f.write(result)
            success_count += 1
            _save_progress(delta_hash, success_count)
        else:
            # local-inference chain (Surface llama-server + Pi fallback) is
            # unavailable. Circuit-break so we don't retry the rest and spam
            # Telegram. Progress is saved, so the next run resumes here.
            local_inference_down = True
            skipped = total_chunks - i
            logger.warning(
                f"Local inference (Surface RPC + Pi) unavailable at chunk {i+1} — "
                f"skipping {skipped} remaining chunk(s). Progress saved; delta "
                f"preserved for next run."
            )
            break
                
    if success_count == total_chunks:
        open(delta_file, 'w').close()
        _save_progress(delta_hash, 0)
        logger.info("Delta Indexing Complete. Output appended to mega_index.md.")
    else:
        logger.warning(f"Only {success_count}/{total_chunks} chunks were indexed. Delta file was NOT cleared to prevent data loss.")

if __name__ == "__main__":
    asyncio.run(run_indexing())
