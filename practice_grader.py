"""
practice_grader.py — Feature 5: Automated Practice Test Grading

When the user sends a photo of completed practice problems:
1. OCR the image (local Tesseract — PII safe)
2. Extract problems and user answers via agy Vision (local agy — PII safe)
3. Compare against answer key from study guide knowledge base
4. Grade and log weak topics to knowledge_gaps/ for nightly targeting
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional
from config import (
    KNOWLEDGE_GAPS_DIR, PRIVATE_RESEARCH_DIR, PRIVATE_STUDY_GUIDES_DIR,
)
KNOWLEDGE_BASE_DIR = PRIVATE_RESEARCH_DIR
STUDY_GUIDES_DIR = PRIVATE_STUDY_GUIDES_DIR

logger = logging.getLogger(__name__)
GAPS_DIR = KNOWLEDGE_GAPS_DIR
MAX_IMAGE_BYTES = 12 * 1024 * 1024


def grade_practice_test(image_path: str, topic: str = "") -> str:
    """
    Grade a practice test from a photo.

    Args:
        image_path: Path to the downloaded image
        topic: Optional topic hint (e.g. "SAT Math", "Geometry")

    Returns:
        Formatted grading results for Telegram
    """
    image = Path(image_path)
    if not image.is_file() or image.is_symlink() or image.stat().st_size > MAX_IMAGE_BYTES:
        return "❌ I couldn’t safely read that image. Please send a fresh image under 12MB."

    # Step 1: Basic OCR for raw text
    raw_ocr = _ocr_image(image_path)

    # Step 2: Use agy Vision to structured-extract problems and answers
    extract_prompt = (
        f"You are a practice test grader. The user uploaded a photo of completed practice problems"
        f"{f' on the topic: {topic}' if topic else ''}.\n\n"
        f"Here is the raw OCR text from the image:\n{raw_ocr[:10000]}\n\n"
        f"Your job is to:\n"
        f"1. Identify each distinct problem number and the user's answer\n"
        f"2. For math problems, try to determine if the work shown is correct\n\n"
        f"Output ONLY a JSON array like this:\n"
        f"[{{\"problem\": 1, \"user_answer\": \"x=5\", \"work_shown\": \"brief description\"}},\n"
        f" {{\"problem\": 2, \"user_answer\": \"B\", \"work_shown\": \"...\"}}]\n"
        f"If you cannot parse any problems, return an empty array []."
    )

    # OCR and answers can contain private school data.  agy/Gemini is cloud
    # hosted, so grading remains local-only even when it is available.
    from llm_router import Sensitivity, call_local_rpc_result

    result = call_local_rpc_result(
        prompt=extract_prompt,
        max_tokens=1_200,
        timeout=120,
        allow_cloud=False,
        sensitivity=Sensitivity.PERSONAL,
    )
    extracted_json = result.text if result.ok else ""

    # Parse the extraction
    problems = _safe_parse_json(extracted_json)
    if problems is None:
        return "❌ I couldn't parse the problems from the photo. Try a clearer image or tell me the topic."

    if not problems:
        return "❌ No problems detected. Make sure the photo is well-lit and shows the full page."

    # Step 3: Find answer key in study guides
    answer_key = _find_answer_key(topic, problems)
    if not answer_key:
        # No stored answer key — just list what we found
        return _format_no_key_response(problems)

    # Step 4: Grade each problem
    results = _grade_problems(problems, answer_key)

    # Step 5: Log weak topics
    _log_weak_topics(results, topic)

    # Step 6: Format results
    return _format_grading_results(results, topic)


def _ocr_image(image_path: str) -> str:
    """Basic OCR via Tesseract. Local only — PII safe."""
    try:
        import pytesseract
        from PIL import Image
        with Image.open(image_path) as image:
            return pytesseract.image_to_string(image)
    except Exception as e:
        logger.error(f"OCR failed: {e}")
        return ""


def _safe_parse_json(text: str) -> Optional[list[dict]]:
    """Try to extract JSON from text that might have extra content."""
    import re
    # Try direct parse
    try:
        value = json.loads(text)
        return _validate_problems(value)
    except Exception:
        pass
    # Try to find JSON array in text
    match = re.search(r'\[[\s\S]*?\]', text)
    if match:
        try:
            return _validate_problems(json.loads(match.group()))
        except Exception:
            pass
    return None


def _validate_problems(value: object) -> Optional[list[dict]]:
    """Accept a bounded schema; never merge arbitrary model-produced keys."""
    if not isinstance(value, list) or len(value) > 50:
        return None
    problems: list[dict] = []
    seen: set[int] = set()
    for item in value:
        if not isinstance(item, dict):
            return None
        number = item.get("problem")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 1_000 or number in seen:
            return None
        answer = item.get("user_answer", "")
        work = item.get("work_shown", "")
        if not isinstance(answer, str) or not isinstance(work, str):
            return None
        problems.append({"problem": number, "user_answer": answer[:200], "work_shown": work[:1_000]})
        seen.add(number)
    return problems


def _find_answer_key(topic: str, problems: list) -> Optional[dict]:
    """
    Search study guides and knowledge base for matching practice problems.
    Returns dict of {problem_number: correct_answer} or None.
    """
    requested = {int(problem["problem"]) for problem in problems}
    topic_slug = _slug(topic) if topic else ""
    candidates: list[tuple[int, dict]] = []
    for directory in (STUDY_GUIDES_DIR, KNOWLEDGE_BASE_DIR):
        if not directory.exists():
            continue
        for guide_file in directory.glob("*.md"):
            if guide_file.is_symlink():
                continue
            if topic_slug and topic_slug not in _slug(guide_file.stem):
                continue
            try:
                key = _extract_answers_from_guide(
                    guide_file.read_text(encoding="utf-8", errors="replace")[:1_000_000], problems
                )
            except OSError:
                continue
            if key:
                candidates.append((len(requested.intersection(key)), key))
    if not candidates:
        return None
    # Pick one strongest source rather than combining same question numbers
    # from unrelated guides, which previously created fabricated answer keys.
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    return candidates[0][1]


def _extract_answers_from_guide(content: str, problems: list) -> Optional[dict]:
    """Extract a practice exam answer key section from a markdown guide."""
    import re

    # Look for "## Practice Exam" or "## Answer Key" section
    sections = re.split(r'^#{2,3}\s+', content, flags=re.MULTILINE)
    for section in sections:
        if "practice exam" in section.lower() or "answer key" in section.lower() or "practice problem" in section.lower():
            # Try to extract numbered Q&A pairs
            answers = {}
            matches = re.findall(r'(\d+)\)[\s:]*?(?:Answer:\s*)?([A-D]|(?:\d+(?:\.\d+)?)|(?:[x=].*?))', section, re.IGNORECASE)
            for num, ans in matches:
                answers[int(num)] = ans.strip()
            return answers if answers else None
    return None


def _grade_problems(problems: list, answer_key: dict) -> list:
    """Grade each problem against the answer key."""
    results = []
    for prob in problems:
        num = prob.get("problem", 0)
        user_ans = str(prob.get("user_answer", "")).strip()
        correct = answer_key.get(num, "")

        result = {
            "problem": num,
            "user_answer": user_ans,
            "correct_answer": correct,
            "work_shown": prob.get("work_shown", ""),
        }

        if isinstance(correct, str) and "explain" in correct.lower():
            # Open-ended: correctness is subjective, skip auto-grade
            result["status"] = "review"
        else:
            # Normalize for comparison
            norm_user = user_ans.lower().strip().replace(" ", "").replace("=", "")
            norm_correct = str(correct).lower().strip().replace(" ", "").replace("=", "")
            result["status"] = "correct" if norm_user == norm_correct else "wrong"

        results.append(result)
    return results


def _log_weak_topics(results: list, topic: str):
    """Log wrong answers to knowledge_gaps/ for nightly targeting."""
    topic_slug = _slug(topic) or "general"
    gaps_file = GAPS_DIR / f"{topic_slug}.txt"

    wrong = [r for r in results if r["status"] == "wrong"]
    if not wrong:
        return

    GAPS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(gaps_file, "a", encoding="utf-8") as f:
        try:
            os.chmod(gaps_file, 0o600)
        except OSError:
            pass
        from datetime import datetime
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        f.write(f"\n--- {date_str} ---\n")
        for r in wrong:
            f.write(f"Problem {r['problem']}: answered '{r['user_answer']}' "
                    f"(correct: {r['correct_answer']})\n")
            if r["work_shown"]:
                f.write(f"  Work shown: {r['work_shown']}\n")

    logger.info(f"Logged {len(wrong)} wrong answers to {gaps_file}")


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")[:80]


def _format_grading_results(results: list, topic: str) -> str:
    """Format grading results for Telegram."""
    correct_count = sum(1 for r in results if r["status"] == "correct")
    wrong_count = sum(1 for r in results if r["status"] == "wrong")
    review_count = sum(1 for r in results if r["status"] == "review")
    total = len(results)

    pct = round(correct_count / total * 100) if total > 0 else 0

    lines = [
        f"📝 **Practice Test Results** — {topic or 'General'}",
        f"Score: **{correct_count}/{total}** ({pct}%)",
        f"✅ {correct_count} correct | ❌ {wrong_count} wrong | ⚠️ {review_count} need review",
        "",
    ]

    # Show wrong answers with corrections
    wrong = [r for r in results if r["status"] == "wrong"]
    if wrong:
        lines.append("**Wrong answers:**")
        for r in wrong:
            lines.append(f"  Q{r['problem']}: you said `{r['user_answer']}` → "
                         f"correct is `{r['correct_answer']}`")

    # Logged message
    if wrong:
        lines.append(f"\n📝 Logged {len(wrong)} gaps — tonight's study guide will target them!")

    # Emoji feedback
    if pct >= 90:
        lines.append("\n🌟 Excellent! You're mastering this!")
    elif pct >= 70:
        lines.append("\n👍 Good work! Review the wrong answers above.")
    else:
        lines.append("\n💪 Keep practicing! I'll focus on this in tonight's study guide.")

    return "\n".join(lines)


def _format_no_key_response(problems: list) -> str:
    """Format response when we found problems but no answer key."""
    lines = [
        f"📝 **Detected {len(problems)} problems:**",
    ]
    for p in problems:
        lines.append(f"  Q{p.get('problem', '?')}: `{p.get('user_answer', '?')}`")

    lines.append("\n⚠️ I couldn't find an answer key for this topic in your study guides.")
    lines.append("I've still logged these — upload a photo of the answer key "
                 "or tell me the topic so I can search for one!")
    return "\n".join(lines)
