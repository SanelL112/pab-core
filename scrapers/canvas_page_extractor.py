import json
import logging
import os
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)

def _agy_model(alias: str) -> str:
    try:
        import sys
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        from llm_router import _resolve_agy_model
        return _resolve_agy_model(alias)
    except Exception:
        return "Gemini 3.5 Flash (Medium)"

def extract_assignments_from_html(course_id: str, course_name: str, page_title: str, page_url: str, html_body: str) -> list[dict]:
    """Uses the CLI AI to extract assignments from page HTML."""
    import re
    # simple strip html to save tokens
    text = re.sub(r'<[^>]+>', ' ', html_body)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return []

    prompt = f"""You are an AI assistant helping a student organize their calendar.
Below is the text extracted from a Canvas course page titled '{page_title}' for the course '{course_name}'.
Extract any upcoming tests, quizzes, readings, or assignments along with their due dates.
Assume the current year is {datetime.now().year}.
Respond ONLY with valid JSON in this exact format (no markdown blocks, no extra text):
[
  {{
    "title": "Task Name",
    "due_date": "YYYY-MM-DD",
    "task_type": "Test"
  }}
]
Task types should be one of: Test, Project, Reading, Assignment.
If there are no actionable dates, output ONLY: []

Text:
{text[:8000]}
"""

    agentapi_bin = os.getenv("AGENTAPI_BIN", "/home/sanel/.local/bin/agy")
    
    try:
        result = subprocess.run(
            [agentapi_bin, "--model", _agy_model("flash"), "--print", prompt],
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode != 0:
            logger.error(f"AI extraction failed: {result.stderr}")
            return []
            
        clean_out = result.stdout.strip()
        # strip markdown blocks if they accidentally appear
        if clean_out.startswith("```json"):
            clean_out = clean_out[7:]
        if clean_out.startswith("```"):
            clean_out = clean_out[3:]
        if clean_out.endswith("```"):
            clean_out = clean_out[:-3]
        clean_out = clean_out.strip()

        parsed = json.loads(clean_out)
        if not isinstance(parsed, list):
            return []
            
        # Add required metadata
        for item in parsed:
            item["course"] = course_name
            # For unique external_id, just hash the title + date to prevent duplicates
            import hashlib
            id_str = f"{page_url}-{item.get('title')}-{item.get('due_date')}"
            item["id"] = f"page-{hashlib.md5(id_str.encode()).hexdigest()[:12]}"
            item["url"] = f"https://forsyth.instructure.com/courses/{course_id}/pages/{page_url}"
            item["official"] = False # Must be approved as a proposal
            
        return parsed
    except Exception as e:
        logger.error(f"Error parsing AI response for page {page_title}: {e}")
        return []
