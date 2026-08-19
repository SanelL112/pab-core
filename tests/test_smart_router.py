import pytest
from bot.smart_router import classify_query, RoutingDecision


def test_classify_greeting_routes_local():
    d = classify_query("Hello there!")
    assert d.target_engine == "local"
    assert d.is_basic is True
    assert d.needs_rag is False


def test_classify_basic_arithmetic_routes_local():
    d = classify_query("calculate 12 * 8")
    assert d.target_engine == "local"
    assert d.is_basic is True


def test_classify_coding_routes_qwen():
    d = classify_query("def parse_json_lines(file_path): pass")
    assert d.target_engine == "qwen-coder"
    assert d.is_basic is False


def test_classify_tutoring_and_math():
    d = classify_query("Can you help me solve this calculus integral step by step?")
    assert d.target_engine == "gemini-pro"
    assert d.mode == "tutor"
    assert d.is_basic is False


def test_classify_canvas_and_onenote_triggers_rag():
    d = classify_query("What does the syllabus on Canvas say about the chemistry midterm?")
    assert d.needs_rag is True
    assert "Canvas" in d.search_query or "chemistry" in d.search_query.lower()


def test_classify_drill_mode():
    d = classify_query("Give me a challenging SAT question on triangles and quiz me")
    assert d.mode == "drill"
    assert d.target_engine == "gemini-pro"


def test_classify_pii_forces_local():
    # Simulated sensitive data
    d = classify_query("My social security number is 000-12-3456 and password is test")
    assert d.target_engine == "local"
    assert d.is_basic is False
    assert "PII detected" in d.reason
