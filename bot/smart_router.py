"""Autonomous Smart Router — classifies queries, triggers vector RAG, and dispatches inference.

Operates with sub-millisecond heuristic classification and optional local model validation
(LFM 1.2B / Qwen 0.5B) to route tasks between Local hardware and the Online Provider Pool.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from utils import check_pii

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoutingDecision:
    target_engine: str  # "local" | "gemini-flash" | "gemini-pro" | "qwen-coder" | "llama3.3"
    mode: str           # "default" | "tutor" | "quick" | "drill"
    needs_rag: bool     # True if OneNote, Canvas, or Drive notes should be retrieved
    search_query: str   # Extracted search query for vector search
    is_basic: bool      # True if query should be answered exclusively by local model
    reason: str         # Human-readable explanation of why this route was selected


_GREETINGS = {
    "hi", "hello", "hey", "yo", "sup", "howdy", "good morning", "good evening",
    "thanks", "thank you", "bye", "goodbye", "help",
}

_BASIC_PATTERNS = [
    r"^(?:what time|what date|what day|who are you|what can you do)",
    r"^(?:convert \d+|calculate \d+\s*[\+\-\*\/]\s*\d+)",
    r"^(?:my tasks|what do i have|schedule|digest|status|server|uptime)",
    r"^(?:ping|echo\b)",
]

_RAG_KEYWORDS = (
    "canvas", "onenote", "google drive", "google doc", "classroom", "notes",
    "syllabus", "assignment", "homework", "due date", "teacher", "class",
    "lecture", "reading", "worksheet", "problem set", "exam", "quiz", "sat", "act",
)

_CODE_KEYWORDS = (
    "def ", "class ", "function ", "import ", "const ", "let ", "var ",
    "python", "javascript", "typescript", "bash", "shell", "sql", "regex",
    "bug", "traceback", "syntax error", "refactor", "algorithm", "git", "docker",
)

_HEAVY_REASONING_KEYWORDS = (
    "derive", "proof", "prove", "integral", "derivative", "calculus", "physics",
    "chemistry", "step-by-step", "solve the equation", "essay", "thesis",
    "deep dive", "compare and contrast", "why does",
)

_TUTOR_KEYWORDS = (
    "help me solve", "walk me through", "guide me", "how do i solve",
    "explain step by step", "i don't understand", "i'm stuck", "tutor me",
)

_DRILL_KEYWORDS = (
    "quiz me", "test me", "practice question", "sat question", "act question",
    "ap question", "drill", "flashcard",
)


def classify_query(user_message: str) -> RoutingDecision:
    """Classify an incoming query and return a structured RoutingDecision."""
    clean = user_message.strip()
    lowered = clean.lower()
    words = lowered.split()
    clean_words = [re.sub(r"[^\w]", "", w) for w in words if re.sub(r"[^\w]", "", w)]

    # 1. PII Check — strictly local
    is_public, _, pii_types = check_pii(clean)
    if not is_public:
        return RoutingDecision(
            target_engine="local",
            mode="default",
            needs_rag=True,
            search_query=clean[:100],
            is_basic=False,
            reason=f"PII detected ({', '.join(pii_types)}); enforcing private local execution",
        )

    # 2. Basic Query / Small Talk — strictly local (saves cloud tokens & fast)
    if len(clean_words) <= 3 and any(w in _GREETINGS for w in clean_words):
        return RoutingDecision(
            target_engine="local",
            mode="default",
            needs_rag=False,
            search_query="",
            is_basic=True,
            reason="Simple greeting / small talk served by local model",
        )

    if any(re.search(pat, lowered) for pat in _BASIC_PATTERNS):
        return RoutingDecision(
            target_engine="local",
            mode="quick",
            needs_rag="task" in lowered or "schedule" in lowered or "digest" in lowered,
            search_query=clean[:80],
            is_basic=True,
            reason="Basic lookup / status check served by local model",
        )

    # 3. Determine RAG necessity
    needs_rag = any(k in lowered for k in _RAG_KEYWORDS)
    search_query = clean[:120] if needs_rag else ""

    # 4. Determine Persona / Study Mode
    if any(k in lowered for k in _TUTOR_KEYWORDS):
        mode = "tutor"
    elif any(k in lowered for k in _DRILL_KEYWORDS):
        mode = "drill"
    elif len(words) <= 6 and not any(k in lowered for k in _HEAVY_REASONING_KEYWORDS):
        mode = "quick"
    else:
        mode = "default"

    # 5. Determine Target Engine
    if "```" in clean or any(k in lowered for k in _CODE_KEYWORDS):
        target_engine = "qwen-coder"
        reason = "Coding / script detected; routing to coding specialist"
    elif any(k in lowered for k in _HEAVY_REASONING_KEYWORDS) or mode in ("tutor", "drill"):
        target_engine = "gemini-pro"
        reason = "Complex reasoning / tutoring detected; routing to Gemini Pro"
    else:
        target_engine = "gemini-flash"
        reason = "General query; routing to Google Gemini Flash"

    return RoutingDecision(
        target_engine=target_engine,
        mode=mode,
        needs_rag=needs_rag,
        search_query=search_query,
        is_basic=False,
        reason=reason,
    )
