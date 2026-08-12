import os
import logging
import asyncio
import tempfile
from pathlib import Path

from activity_log import log_nightly
from scrapers.batch_results import BatchResult, BatchStatus, validate_generated_text

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_BRAIN_STRUCTURE = (
    "upcoming deadlines & tasks",
    "current study topics",
    "key insights",
)


def _deterministic_brain(raw_text: str) -> str:
    """Produce a useful local brain when the local model refuses or times out."""
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    lines = [line for line in lines if not line.lower().startswith(("i'm sorry", "i’m sorry", "i can't", "i can’t"))]
    tasks = [line for line in lines if "due" in line.lower() or "deadline" in line.lower()][:12]
    topics = [line for line in lines if any(word in line.lower() for word in (
        "calculus", "photosynthesis", "sat", "act", "geometry", "biology", "academic integrity",
    ))][:12]
    insights = lines[-12:]

    def section(title: str, values: list[str]) -> str:
        return "## " + title + "\n" + "\n".join(
            f"- {value[:500]}" for value in (values or ["No new items captured."])
        )

    return "\n\n".join((
        section("Upcoming Deadlines & Tasks", tasks),
        section("Current Study Topics", topics),
        section("Key Insights", insights),
    ))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, temporary_path = tempfile.mkstemp(prefix=".tmp-brain-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    except Exception:
        try:
            os.unlink(temporary_path)
        except OSError:
            pass
        raise


async def consolidate_memory() -> BatchResult:
    logger.info("Starting local memory consolidation")

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

    from config import CHAT_HISTORY_DIR, COMBINED_SUMMARIES_FILE

    # Gather raw text
    raw_text = ""

    # 1. Read combined_summaries.txt
    summaries_file = Path(COMBINED_SUMMARIES_FILE)
    if summaries_file.exists():
        raw_text += "\n--- DAILY SUMMARIES AND NOTES ---\n" + summaries_file.read_text(
            encoding="utf-8", errors="replace"
        )[-50_000:]

    # 2. Read chat_history files
    for chat_file in Path(CHAT_HISTORY_DIR).glob("chat_history_*.txt"):
        if chat_file.is_file() and not chat_file.is_symlink():
            raw_text += f"\n--- CHAT HISTORY ({chat_file.name}) ---\n" + chat_file.read_text(
                encoding="utf-8", errors="replace"
            )[-8_000:]

    if not raw_text.strip():
        logger.info("No raw memory to consolidate tonight.")
        return BatchResult(BatchStatus.OK, detail="no memory input")

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

    # This corpus contains private school/chat data.  It is local-only and must
    # never fall back to agy/Gemini or another cloud provider.
    from llm_router import call_local_rpc
    try:
        raw_brain = await asyncio.to_thread(
            call_local_rpc,
            prompt=prompt,
            max_tokens=2048,
            temperature=0.2,
            timeout=300,
            allow_cloud=False,
        )
    except Exception as e:
        logger.error("Local memory consolidation failed: %s: %s", type(e).__name__, e)
        return BatchResult(BatchStatus.ERROR, detail=f"{type(e).__name__}: {e}")

    brain_result = validate_generated_text(
        raw_brain,
        min_chars=200,
        required_markers=_BRAIN_STRUCTURE,
    )
    if not brain_result.ok:
        logger.warning(
            "Memory model output rejected (%s): %s; using deterministic local consolidation",
            brain_result.status.value,
            brain_result.detail,
        )
        brain_result = BatchResult(
            BatchStatus.OK,
            text=_deterministic_brain(raw_text),
            detail="deterministic fallback used",
        )

    # Phase 1: Write brain to file
    try:
        from config import CURATED_BRAIN_FILE

        brain_file = Path(CURATED_BRAIN_FILE)
        existing_brain = ""
        if brain_file.exists():
            with brain_file.open("r", encoding="utf-8", errors="replace") as f:
                existing_brain = f.read()

        existing_result = validate_generated_text(
            existing_brain,
            min_chars=200,
            required_markers=_BRAIN_STRUCTURE,
        )
        final_brain = brain_result.text
        if existing_result.ok:
            # Merge old and new
            merge_prompt = (
                "Merge the old brain and new daily insights into one cohesive Markdown document. "
                "Preserve the required sections Upcoming Deadlines & Tasks, Current Study Topics, and Key Insights.\n\n"
                f"OLD BRAIN:\n{existing_result.text}\n\nNEW INSIGHTS:\n{brain_result.text}"
            )
            try:
                merged_out = await asyncio.to_thread(
                    call_local_rpc,
                    prompt=merge_prompt,
                    max_tokens=2048,
                    temperature=0.2,
                    timeout=300,
                    allow_cloud=False,
                )
            except Exception as e:
                logger.warning("Local brain merge failed; publishing the validated daily brain: %s", e)
                merged_out = ""
            merge_result = validate_generated_text(
                merged_out,
                min_chars=200,
                required_markers=_BRAIN_STRUCTURE,
            )
            if merge_result.ok:
                final_brain = merge_result.text
            else:
                logger.warning("Merged brain rejected (%s); publishing the validated daily brain", merge_result.detail)

        _atomic_write(brain_file, final_brain)
    except Exception as e:
        logger.error(f"Failed to write brain file: {e}")
        return BatchResult(BatchStatus.ERROR, detail=f"brain publication failed: {e}")

    # Phase 2: Incremental local index.  Historical exports/downloads are
    # intentionally separate operator-triggered jobs: they are external I/O,
    # not a side effect of consolidating local memory.
    logger.info("Running incremental local historical index")
    try:
        from scrapers.offline_indexer import run_indexing
        index_result = await run_indexing()
        if not index_result.ok:
            logger.warning(
                "Nightly historical index retained its input (%s): %s",
                index_result.status.value,
                index_result.detail,
            )
    except Exception as e:
        logger.error(f"Nightly indexer failed: {e}")

    # Phase 3: Embedding Index Rebuild
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
        log_nightly("embedding_indexer", "error", {"message": str(e)[:80]})

    logger.info("Daily pipeline complete.")

    # Phase 4: Bound raw logs without unsafe in-place truncation.
    try:
        from utils import rotate_file_if_needed

        rotate_file_if_needed(summaries_file, 90_000)
        logger.info("Memory consolidation complete")
    except Exception as e:
        logger.error(f"Failed to trim raw logs: {e}")

    return BatchResult(BatchStatus.OK, text=final_brain, detail="memory consolidation complete")

if __name__ == "__main__":
    result = asyncio.run(consolidate_memory())
    raise SystemExit(0 if result.ok else 1)
