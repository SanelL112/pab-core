import os
import json
import logging
from dataclasses import dataclass
from pathlib import Path
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import ARCHIVE_DIR, CACHE_DIR, STUDY_DATABASE_DIR
from scrapers.batch_results import validate_generated_text

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# One guide is intentionally finite.  The previous 500k budget could occupy
# the inference cluster for hours and starve interactive requests.
MAX_NIGHTLY_TOKENS = 120_000
DEFAULT_OUTLINE = (
    "Chapter 1: Core Concepts & Vocabulary",
    "Chapter 2: Essential Formulas & Theorems",
    "Chapter 3: Deep-Dive Explanations Part 1",
    "Chapter 4: Deep-Dive Explanations Part 2",
    "Chapter 5: Advanced Strategies & Tactics",
    "Chapter 6: Step-by-Step Problem Solving Frameworks",
    "Chapter 7: Real-World Applications",
    "Chapter 8: Action Plan & Memorization Checklist",
    "Chapter 9: Common Mistakes & Pitfalls",
    "Chapter 10: Master Practice Exam",
)


@dataclass
class TokenBudget:
    """Bound all local generation work performed by one guide build."""

    limit: int
    used: int = 0

    def reserve(self, tokens: int) -> bool:
        tokens = max(0, int(tokens))
        if self.used + tokens > self.limit:
            return False
        self.used += tokens
        return True


def _valid_outline(value: object) -> list[str] | None:
    """Accept only a bounded, predictable chapter plan from model output."""

    if not isinstance(value, list) or len(value) != len(DEFAULT_OUTLINE):
        return None
    if not all(isinstance(item, str) and 1 <= len(item.strip()) <= 120 for item in value):
        return None
    outline = [item.strip() for item in value]
    if len({item.casefold() for item in outline}) != len(outline):
        return None
    if sum("practice" in item.casefold() for item in outline) != 1:
        return None
    return outline


def _without_thoughts(text: str) -> str:
    import re

    return re.sub(r"<thought>.*?</thought>", "", text, flags=re.DOTALL).strip()

MEGA_STUDY_PROMPT = """You are an elite academic tutor. I am trying to build a master study guide for the topic: "{topic}".
I have autonomously pulled in context from multiple sources, including the teacher's handwritten notes, web articles, and YouTube transcripts. Synthesize all of this information into the ultimate, extremely comprehensive, beautifully formatted Markdown study guide.

--- TEACHER'S HANDWRITTEN NOTES ---
{pdf_text}

--- YOUTUBE TRANSCRIPTS ({yt_title}) ---
{transcript}

--- WEB ARTICLE SUMMARIES ({web_title}) ---
{web_text}

--- YOUR INSTRUCTIONS ---
Using the provided context and your own vast knowledge, you MUST be as exhaustive and detailed as possible. Do not skip over nuances.
YOUR OUTPUT MUST BE EXTREMELY LONG. THERE IS NO MAXIMUM LIMIT. The guide can be 50+ pages if necessary. Do not restrict yourself. Expand endlessly on every single point, breaking them down into microscopic details.
HOWEVER, because you have an output token limit, you MUST spend 90% of your response deeply expanding on Formulas, Theorems, and Explanations. Do NOT waste all your output tokens on practice problems!

You MUST follow this exact strict format:
1. **Core Formulas & Theorems (MASSIVE EXPANSION)**: Break down every single key formula, date, vocabulary word, or core concept. Prioritize the teacher's classroom notes. Expand on this section endlessly.
2. **Deep-Dive Explanations**: Provide incredibly detailed, exhaustive explanations of the topic. Explain the "Why" and the "How".
3. **Advanced Strategies & Tactics**: Create a comprehensive list of strategies, workflow timelines, and exact step-by-step tactics to master the topic.
4. **Action Plan (What I Need To Do)**: Tell me exactly what I need to do next to master this topic. Give me a clear checklist of specific concepts to memorize.
5. **Practice Exam**: Write 5 highly challenging practice problems with a detailed step-by-step answer key at the very bottom.
6. **Topic Contextualization**: Even if the provided notes or PDFs do not explicitly mention "{topic}" by name, you MUST extract their underlying concepts and apply them directly to "{topic}".

CRITICAL FORMATTING RULES: 
- Your final output MUST be a pristine, highly structured, professional study guide.
- Do not include internal monologues, hidden reasoning, or <thought> tags.
"""

def search_web_article(topic: str):
    """Consume only references already validated by ``web_precacher``."""
    slug = "".join(char if char.isalnum() else "_" for char in topic.lower()).strip("_")[:60]
    path = Path(STUDY_DATABASE_DIR) / f"{slug}.md"
    if not path.is_file() or path.is_symlink():
        return None, ""
    try:
        return [{"title": path.stem, "href": "local-public-cache"}], path.read_text(
            encoding="utf-8", errors="replace"
        )[:40_000]
    except OSError:
        return None, ""

def search_images(topic: str):
    """External image URLs are intentionally not injected into private guides."""
    return []

def search_youtube(topic: str):
    """Transcript fetching is not part of the unattended private build path."""
    return None, ""


def generate_mega_guide(topic: str, pdf_text: str = "") -> str:
    """Generates the ultimate chunked study guide."""
    topic = " ".join(str(topic).split())
    if not topic or len(topic) > 120:
        return ""
    logger.info(f"Generating MEGA guide for: {topic} using Chunking Architecture")
    
    # Strip out generic filler words so the scrapers search for the actual subject matter
    search_topic = topic.lower()
    for filler in ["comprehensive", "study guide", "prep guide", "ultimate", "master class", "crash course", "review"]:
        search_topic = search_topic.replace(filler, "")
    search_topic = search_topic.strip()
    if not search_topic:
        search_topic = topic # Fallback just in case
    
    yt_meta, yt_text = search_youtube(search_topic)
    web_meta, web_text = search_web_article(search_topic)
    
    # Pull in the user's internal notes from Canvas/Docs/Classroom and Extracted PDFs
    internal_notes = ""
    notes_file = CACHE_DIR / "combined_summaries.txt"
    pdf_exports_file = CACHE_DIR / "pdf_exports.txt"
    
    if os.path.exists(notes_file):
        try:
            with open(notes_file, "r") as f:
                internal_notes += f.read().replace('\x00', '').strip()
        except Exception:
            pass
            
    if os.path.exists(pdf_exports_file):
        try:
            with open(pdf_exports_file, "r") as f:
                internal_notes += "\n\n" + f.read().replace('\x00', '').strip()
        except Exception:
            pass
            
    delta_export_file = Path(ARCHIVE_DIR) / "delta_export.txt"
    if delta_export_file.is_file() and not delta_export_file.is_symlink():
        try:
            internal_notes += "\n\n" + delta_export_file.read_text(encoding="utf-8", errors="replace").replace('\x00', '').strip()
        except Exception:
            pass
            
    if not internal_notes:
        internal_notes = "None"

    from llm_router import Sensitivity, call_local_rpc_result, estimate_tokens

    # Classroom notes, cached summaries, and uploaded PDFs are private.  This
    # pipeline deliberately uses the local inference chain only.
    token_budget = TokenBudget(MAX_NIGHTLY_TOKENS)

    def _call_local(prompt_text, timeout=3600, max_tokens=4_000):
        """Generate locally while sharing one bounded per-guide budget.

        max_tokens stays modest per call: sustained full-tilt generation on
        the Surface tablet drains its battery faster than charging replenishes
        and has hard-crashed the node mid-build."""
        prompt_tokens = estimate_tokens(prompt_text)
        if not token_budget.reserve(prompt_tokens):
            logger.warning(
                "Token budget exceeded (%s/%s), skipping call",
                token_budget.used,
                token_budget.limit,
            )
            return ""
        result = call_local_rpc_result(
            prompt=prompt_text,
            max_tokens=max_tokens,
            timeout=timeout,
            allow_cloud=False,
            sensitivity=Sensitivity.PERSONAL,
        )
        if not result.ok:
            logger.warning("Local study-guide inference unavailable: %s", result.detail or result.status.value)
            return ""
        text = result.text
        result_tokens = estimate_tokens(text)
        if not token_budget.reserve(result_tokens):
            logger.warning("Study-guide response exceeded the remaining token budget")
            return ""
        logger.info(
            "Local guide token usage: %s/%s (this call: ~%s)",
            token_budget.used,
            token_budget.limit,
            prompt_tokens + result_tokens,
        )
        return text

    logger.info("Assembling and cleaning context payload...")
    
    # Strip null bytes
    pdf_text = pdf_text.replace('\x00', '') if pdf_text else ""
    yt_text = yt_text.replace('\x00', '') if yt_text else ""
    web_text = web_text.replace('\x00', '') if web_text else ""
    internal_notes = internal_notes.replace('\x00', '') if internal_notes else ""
    
    def _chunk_and_summarize(text, label):
        # 12KB chunks (~3k prompt tokens): keeps each Surface generation
        # short so sustained load cannot drain the tablet's battery.
        max_chunk_size = 12_000
        if len(text) > max_chunk_size:
            logger.info(f"Chunking {label} ({len(text)} chars)...")
            summarized = ""
            for i in range(0, len(text), max_chunk_size):
                chunk = text[i:i+max_chunk_size]
                prompt = f"Extract all facts, concepts, formulas, and notes strictly relevant to '{topic}'. Be comprehensive but concise. Ignore unrelated subjects.\n\nSOURCE TEXT ({label} Chunk {i//max_chunk_size + 1}):\n{chunk}"
                logger.info(f"Summarizing {label} chunk {i//max_chunk_size + 1} / {(len(text)//max_chunk_size)+1}...")
                summary = _call_local(prompt, max_tokens=1_200)
                if summary:
                    summarized += summary + "\n\n"
                    
            # The Final Reduce (Coherence) Pass
            logger.info(f"Synthesizing all {label} chunks into a coherent master document...")
            synthesis_prompt = f"You are an expert synthesizer. I have provided multiple summaries of '{topic}' below. Please rewrite them into one single, highly coherent, deduplicated master reference document. Do not leave any facts out.\n\nSUMMARIES:\n{summarized}"
            final_coherent_doc = _call_local(synthesis_prompt)
            
            return final_coherent_doc if final_coherent_doc else summarized
            
        return text

    pdf_text = _chunk_and_summarize(pdf_text, "PDF Notes")
    yt_text = _chunk_and_summarize(yt_text, "YouTube Transcripts")
    web_text = _chunk_and_summarize(web_text, "Web Articles")
    internal_notes = _chunk_and_summarize(internal_notes, "Internal Notes")

    source_context = f"""
--- TEACHER'S HANDWRITTEN NOTES & CLASSROOM PDFS ---
{pdf_text if pdf_text else "None"}

--- YOUR PERSONAL NOTES (CANVAS/CLASSROOM/DOCS) ---
{internal_notes}

--- YOUTUBE TRANSCRIPTS ({yt_meta['title'] if yt_meta else 'None'}) ---
{yt_text if yt_text else 'None'}

--- WEB ARTICLE SUMMARIES ({f'{len(web_meta)} sources' if web_meta else 'None'}) ---
{web_text if web_text else 'None'}
"""
    
    image_sources = search_images(search_topic)
    
    # Format the Visual Asset Library for the AI
    visual_asset_library = "VISUAL ASSET LIBRARY (Use these to insert images):\n"
    if image_sources:
        for img in image_sources:
            visual_asset_library += f"- [{img['title']}] URL: {img['image']}\n"
    else:
        visual_asset_library += "(No visual assets found.)\n"
    
    # Re-use web_meta as web_sources for the final citation assembly
    web_sources = web_meta

    # PHASE 1: Generate Master Outline
    logger.info("PHASE 1: Generating Master Outline...")
    outline_prompt = f"""You are an elite academic tutor planning a 50+ page massive study guide for "{topic}".
Here are the sources you have available:
{source_context}

Create a highly detailed 10-chapter outline for this study guide. 
CRITICAL LIMITATION: You may ONLY include exactly ONE chapter for "Practice Problems". The other 9 chapters MUST be purely dedicated to Core Formulas, Deep-Dive Explanations, Advanced Strategies, and Step-by-Step Tactics.
Respond ONLY with a raw JSON array of strings representing the chapter titles. Do not include markdown blocks or any other text.
Example: ["Chapter 1: Introduction to Formulas", "Chapter 2: Advanced Mechanics"]"""
    
    outline_json_str = _call_local(outline_prompt)
    outline = None
    if outline_json_str:
        try:
            # Try to strip markdown JSON blocks if the AI disobeyed
            clean_json = outline_json_str.replace("```json", "").replace("```", "").strip()
            outline = _valid_outline(json.loads(clean_json))
        except Exception as e:
            logger.error(f"Failed to parse outline JSON: {e}")
            
    if outline is None:
        logger.warning("Invalid outline; using the fixed 10-chapter outline.")
        outline = list(DEFAULT_OUTLINE)

    # PHASE 1.5: Condense source_context once to avoid repeating it per-chapter
    condensed_context = source_context
    if len(source_context) > 10000:
        logger.info(f"Condensing source_context ({len(source_context)} chars) for per-chapter use...")
        condensed_context = _call_local(
            f"Condense the following educational source material into a dense, structured reference "
            f"document (max 3000 chars) preserving all key facts, formulas, and concepts for "
            f"the topic '{topic}'. Be comprehensive but concise.\n\nSOURCE MATERIAL:\n{source_context}",
            timeout=120,
        )
        # Fallback: truncate at sentence boundary instead of arbitrary char count
        if not condensed_context:
            # Find last complete sentence within 8000 chars
            truncated = source_context[:8000]
            last_period = max(truncated.rfind('.'), truncated.rfind('!'), truncated.rfind('?'))
            if last_period > 1000:  # If we found a reasonable sentence end
                condensed_context = truncated[:last_period + 1]
            else:
                condensed_context = truncated

    # PHASE 2: Chunked Generation
    logger.info(f"PHASE 2: Generating {len(outline)} Chapters...")
    raw_chunks = {}
    full_guide_content = ""

    for i, chapter in enumerate(outline):
        logger.info(f"Generating Chunk {i+1}/{len(outline)}: {chapter}")
        chunk_prompt = f"""You are an elite academic tutor writing a 50+ page massive study guide for "{topic}".
Here is your condensed reference material:
{condensed_context}

{visual_asset_library}

Your current task is to write ONLY the content for: **{chapter}**

INSTRUCTIONS:
1. Expand on this chapter endlessly. Do not hold back. Break down every microscopic detail, nuance, and explanation.
2. Assume the reader needs an incredibly rigorous, textbook-level deep dive.
3. STRICT PROHIBITION: Unless this specific chapter is explicitly titled "Practice Exam", DO NOT write any practice problems. Spend 100% of your tokens on strategy, theory, formulas, and deep-dive explanations.
4. VISUAL INJECTION: When explaining a visual concept (like geometry, graphs, etc.), explicitly embed an image from the Visual Asset Library using Markdown: `![Alt Text](URL)`. 
5. ABSOLUTELY NO INTERNAL MONOLOGUES or "thinking out loud".
6. Do not include internal planning, hidden reasoning, or <thought> tags.
7. Start your output directly with a Markdown Header for the chapter (e.g. # {chapter}).
"""
        chunk_result = _without_thoughts(_call_local(chunk_prompt))
        chunk_validation = validate_generated_text(chunk_result, min_chars=80)

        if chunk_validation.ok:
            chunk_result = chunk_validation.text
            raw_chunks[chapter] = chunk_result
            full_guide_content += f"\n\n{chunk_result}\n\n---\n"
        else:
            logger.warning(
                "Failed to generate a valid local chunk for %s: %s",
                chapter,
                chunk_validation.detail,
            )
            err_msg = f"\n\n# {chapter}\n\n*(Failed to generate this section)*\n\n---\n"
            raw_chunks[chapter] = err_msg
            full_guide_content += err_msg

    # PHASE 4: Editor-in-Chief Verification Loop
    logger.info("PHASE 4: Editor-in-Chief Verification Loop...")
    polished_guide_content = ""
    for i, chapter in enumerate(outline):
        logger.info(f"Editor Reviewing Chunk {i+1}/{len(outline)}: {chapter}")
        # Only send the current chapter + brief guide structure, NOT the full guide
        chapter_outline = "\n".join(f"- {c}" for c in outline)
        editor_prompt = f"""You are the elite Editor-in-Chief. Below is a raw draft chapter from a study guide on "{topic}".

OUTLINE OF FULL GUIDE:
{chapter_outline}

CURRENT CHAPTER TO REVIEW:
{raw_chunks.get(chapter, "")}

INSTRUCTIONS:
1. Fix all disorganized headers to ensure a clean, hierarchical flow.
2. Verify math equations and fact-check concepts.
3. CRITICAL: Format ALL math using standard `$ x $` for inline math and `$$ x $$` for block math. Do NOT use \\\\( or \\\\[.
4. RUTHLESS PRUNING: Unless this specific chapter is explicitly titled "Practice Exam", you MUST delete all practice questions, multiple-choice problems. Replace them with deep-dive strategy and theory instead.
5. PRESERVE IMAGES: Ensure any markdown images like `![alt](URL)` are perfectly preserved and not broken or removed.
6. Do not include internal scratchpad, hidden reasoning, or <thought> tags.
7. Output ONLY the perfectly polished, final version of {chapter}. Do NOT output other chapters.
"""
        editor_result = _without_thoughts(_call_local(editor_prompt))
        editor_validation = validate_generated_text(editor_result, min_chars=80)

        if editor_validation.ok:
            editor_result = editor_validation.text
            polished_guide_content += f"\n\n{editor_result}\n\n---\n"
        else:
            logger.warning("Editor failed on %s, falling back to unpolished local chunk.", chapter)
            polished_guide_content += f"\n\n{raw_chunks[chapter]}\n\n---\n"

    # PHASE 3: Assembly
    final_message = f"🧠 **ULTIMATE CHUNKED STUDY GUIDE: {topic}**\n\n*(Generated dynamically via a {len(outline)}-part LLM Generation & Verification Pipeline to bypass limits)*\n\n{polished_guide_content}\n\n**Sources Used:**\n- Classroom PDFs (Local Brain Sync)"
    if yt_meta:
        final_message += f"\n- YouTube: {yt_meta['title']}"
        
    if web_sources:
        final_message += "\n\n**Web Articles Scraped:**"
        for s in web_sources:
            final_message += f"\n- [{s['title']}]({s['href']})"
            
    return final_message

def build_guide_for_drive_file(file_id: str, topic: str):
    import tempfile
    from scrapers.google_scraper import download_drive_file
    from scrapers.extract_notes import transcribe_handwritten_pdf
    
    logger.info(f"Building guide for Drive file {file_id} on topic {topic}")
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        path = tmp.name
    
    try:
        if not download_drive_file(file_id, path):
            return "❌ Failed to download the PDF from Google Drive. Ensure I have access."
        transcript = transcribe_handwritten_pdf(path)
        if "Error:" in transcript:
            return "❌ I could not extract readable text from that PDF."
        return generate_mega_guide(topic, pdf_text=transcript)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
