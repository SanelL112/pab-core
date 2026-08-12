import logging
from pathlib import Path

from config import COMBINED_SUMMARIES_FILE, CURATED_BRAIN_FILE, PRIVATE_RESEARCH_DIR


logger = logging.getLogger(__name__)

KB_DIR = Path(PRIVATE_RESEARCH_DIR)


def _local_inference(prompt: str, *, max_tokens: int, timeout: int) -> str:
    """Run private overnight work on owner-controlled inference only."""
    try:
        from llm_router import Sensitivity, call_local_rpc_result

        result = call_local_rpc_result(
            prompt=prompt,
            max_tokens=max_tokens,
            timeout=timeout,
            allow_cloud=False,
            sensitivity=Sensitivity.PERSONAL,
        )
        if result.ok:
            return result.text
        logger.warning("Local overnight inference unavailable: %s", result.detail or result.status.value)
    except Exception as exc:
        logger.warning("Local overnight inference failed: %s", type(exc).__name__)
    return ""

def run_overnight_research():
    """
    Run the overnight research pipeline without exporting private corpus data.
    """
    KB_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    
    brain_file = CURATED_BRAIN_FILE
    summaries = COMBINED_SUMMARIES_FILE
    
    context = ""
    if Path(brain_file).is_file():
        context += Path(brain_file).read_text(encoding="utf-8", errors="replace")[-20_000:] + "\n"
    if Path(summaries).is_file():
        context += Path(summaries).read_text(encoding="utf-8", errors="replace")[-10_000:]
            
    if not context.strip():
        logger.warning("No context found to extract topics from.")
        return

    # ── Step 1: Extract topics with local inference ──────────────────────
    logger.info("Extracting academic topics from private context locally...")
    topic_prompt = (
        "You are an academic topic extractor. Read the following personal context and extract ONLY the academic, general, or professional topics "
        "that require deep-dive research (e.g. 'Quadratic Equations', 'American Revolution', 'Photosynthesis', 'SAT Math').\n"
        "DO NOT include any names, personal info, dates, or specific assignment names (like 'Day 9 Homework'). "
        "Just output a comma-separated list of 3-5 core academic topics found in the text. Nothing else.\n\n"
        f"CONTEXT:\n{context}"
    )
    
    topics_raw = _local_inference(topic_prompt, max_tokens=500, timeout=120)
    if not topics_raw:
        logger.error("Failed to extract topics with local inference")
        return
    if "```" in topics_raw:
        topics_raw = topics_raw.split("```")[1].lstrip("json").lstrip()
    topics = [t.strip() for t in topics_raw.split(",") if t.strip()]
        
    logger.info(f"Extracted topics to research: {topics}")

    # ── Step 2: Research each topic (RPC first, with fallbacks) ──────────
    for topic in topics[:5]:  # Max 5 topics a night
        safe_topic = "".join(c for c in topic if c.isalnum() or c in " -_").strip()
        kb_file = KB_DIR / f"{safe_topic.replace(' ', '_').lower()}.md"
        
        if kb_file.exists():
            logger.info(f"Skipping {topic}, already researched.")
            continue
            
        logger.info(f"Researching topic: {topic}")
        
        research_text = _research_topic_with_fallbacks(topic, context)
        
        if research_text and len(research_text) > 500:
            temporary = kb_file.with_suffix(".tmp")
            temporary.write_text(research_text, encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(kb_file)
            kb_file.chmod(0o600)
            logger.info(f"Saved research guide for {topic} ({len(research_text)} chars).")
        else:
            logger.warning(f"Research for {topic} failed or was too short — all fallbacks exhausted.")


def _research_topic_with_fallbacks(topic: str, context: str) -> str:
    """
    Research a single topic with owner-controlled local inference.
    """
    research_prompt = (
        f"Create a comprehensive study guide for: {topic}\n\n"
        f"Include:\n"
        f"- Key concepts and definitions\n"
        f"- Important formulas, principles, or dates\n"
        f"- Step-by-step problem-solving approaches\n"
        f"- Common mistakes and how to avoid them\n"
        f"- Practice problem types with solutions\n\n"
        f"Context from user's notes:\n{context[:5000]}\n\n"
        f"Be thorough and detailed — this runs overnight."
    )

    result = _local_inference(research_prompt, max_tokens=4_000, timeout=600)
    if len(result) > 500:
        logger.info("Local research succeeded for %r (%s chars)", topic, len(result))
        return result
    logger.warning("Local research returned insufficient content for %r", topic)
    return ""

if __name__ == "__main__":
    run_overnight_research()
