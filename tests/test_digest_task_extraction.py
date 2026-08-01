"""Regression tests for digest task/topic extraction.

The digest prompt asks the model for bare marker lines (``TASKS_JSON:[...]``).
Small local models answer with equivalent but differently-shaped payloads.  The
original parser used ``re.search(r'TASKS_JSON:...')``, which a quote between the
marker and the colon defeats, so ``{"TASKS_JSON": [...]}`` silently produced
zero tasks and nothing was ever written to Notion.

These tests pin every shape seen in production plus the failure modes that must
not regress.
"""
from __future__ import annotations

import ai_processor


class TestBareMarkerForm:
    """The documented format must keep working."""

    def test_bare_marker_single_line(self):
        text = 'Digest body here.\nTASKS_JSON:[{"id":"1","title":"Read chapter 4","due_date":"2026-09-01"}]'
        tasks, digest = ai_processor._parse_llm_tasks(text)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Read chapter 4"
        assert tasks[0]["due_date"] == "2026-09-01"
        assert "TASKS_JSON" not in digest
        assert "Digest body here." in digest

    def test_bare_marker_with_space_after_colon(self):
        text = 'TASKS_JSON: [{"title":"Lab writeup"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert [t["title"] for t in tasks] == ["Lab writeup"]

    def test_multiline_payload(self):
        text = 'Body\nTASKS_JSON:[\n  {"title":"A"},\n  {"title":"B"}\n]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert [t["title"] for t in tasks] == ["A", "B"]


class TestQuotedKeyForm:
    """The shape that caused the outage: marker as a JSON object key."""

    def test_quoted_marker_key_in_fenced_block(self):
        # Verbatim shape observed from the Pi Ollama fallback model.
        text = (
            "```json\n"
            "{\n"
            '  "TASKS_JSON": [\n'
            '    {"id": "1", "title": "Study Calculus Limits", "priority": "medium",\n'
            '     "status": "Not started", "due_date": null},\n'
            '    {"id": "2", "title": "Prepare for the Second Mock Test", "priority": "medium"}\n'
            "  ]\n"
            "}\n"
            "```"
        )
        tasks, digest = ai_processor._parse_llm_tasks(text)
        assert len(tasks) == 2, "quoted marker key must not silently yield zero tasks"
        assert tasks[0]["title"] == "Study Calculus Limits"
        assert tasks[1]["title"] == "Prepare for the Second Mock Test"
        assert "TASKS_JSON" not in digest

    def test_quoted_marker_without_fence(self):
        text = '{"TASKS_JSON": [{"title": "Essay draft"}]}'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert [t["title"] for t in tasks] == ["Essay draft"]

    def test_single_quoted_marker(self):
        text = "Body\n'TASKS_JSON' : [{\"title\": \"Quiz review\"}]"
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert [t["title"] for t in tasks] == ["Quiz review"]

    def test_old_regex_would_have_failed(self):
        """Documents the exact defect so the fix is not silently reverted."""
        import re
        broken = '{"TASKS_JSON": [{"title": "x"}]}'
        assert re.search(r'TASKS_JSON:(.*?)$', broken, re.DOTALL) is None
        tasks, _ = ai_processor._parse_llm_tasks(broken)
        assert len(tasks) == 1


class TestNestedAndTrickyPayloads:
    def test_nested_objects_do_not_truncate(self):
        text = 'TASKS_JSON:[{"title":"A","meta":{"inner":{"deep":1}}},{"title":"B"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert [t["title"] for t in tasks] == ["A", "B"]

    def test_bracket_inside_string_literal(self):
        text = 'TASKS_JSON:[{"title":"Read [Chapter 4] now"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert tasks[0]["title"] == "Read [Chapter 4] now"

    def test_escaped_quote_inside_title(self):
        text = 'TASKS_JSON:[{"title":"Ohm\\"s law lab"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert len(tasks) == 1

    def test_single_object_not_wrapped_in_list(self):
        text = 'TASKS_JSON:{"title":"Lone task"}'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert [t["title"] for t in tasks] == ["Lone task"]

    def test_plain_string_items(self):
        text = 'TASKS_JSON:["Finish poster", "Email coach"]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert [t["title"] for t in tasks] == ["Finish poster", "Email coach"]


class TestFieldNormalization:
    def test_alias_keys_are_mapped(self):
        text = 'TASKS_JSON:[{"name":"Break Assignment 1","deadline":"2026-07-01","link":"http://x","subject":"SAT"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        task = tasks[0]
        assert task["title"] == "Break Assignment 1"
        assert task["due_date"] == "2026-07-01"
        assert task["url"] == "http://x"
        assert task["course"] == "SAT"

    def test_placeholder_due_dates_become_none(self):
        for placeholder in ("No due date", "TBD", "n/a", "null", "unknown", ""):
            text = f'TASKS_JSON:[{{"title":"T","due_date":"{placeholder}"}}]'
            tasks, _ = ai_processor._parse_llm_tasks(text)
            assert tasks[0]["due_date"] is None, placeholder

    def test_datetime_is_trimmed_to_date(self):
        text = 'TASKS_JSON:[{"title":"T","due_date":"2026-07-01T13:00:00"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert tasks[0]["due_date"] == "2026-07-01"

    def test_defaults_are_supplied(self):
        text = 'TASKS_JSON:[{"title":"T"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert tasks[0]["priority"] == "unknown"
        assert tasks[0]["status"] == "Not started"
        assert tasks[0]["id"]

    def test_unknown_keys_are_dropped(self):
        # add_task_to_notion takes fixed kwargs; a stray key would raise TypeError.
        text = 'TASKS_JSON:[{"title":"T","assigned_to":"Ashish","notes":"x"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert "assigned_to" not in tasks[0]
        assert "notes" not in tasks[0]

    def test_titleless_and_malformed_items_are_skipped(self):
        text = 'TASKS_JSON:[{"title":""},{"notes":"no title"},42,null,{"title":"Real"}]'
        tasks, _ = ai_processor._parse_llm_tasks(text)
        assert [t["title"] for t in tasks] == ["Real"]


class TestNoPayload:
    def test_absent_marker_returns_empty_and_preserves_text(self):
        text = "Just a normal digest with no machine payload."
        tasks, digest = ai_processor._parse_llm_tasks(text)
        assert tasks == []
        assert digest == text

    def test_malformed_json_does_not_raise(self):
        text = 'TASKS_JSON:[{"title": "unterminated'
        tasks, digest = ai_processor._parse_llm_tasks(text)
        assert tasks == []
        assert isinstance(digest, str)

    def test_empty_list_is_respected(self):
        text = "All caught up.\nTASKS_JSON:[]"
        tasks, digest = ai_processor._parse_llm_tasks(text)
        assert tasks == []
        assert "TASKS_JSON" not in digest


class TestTopicExtraction:
    def test_bare_topics(self):
        text = 'STUDY_TOPICS_JSON:["Calculus Limits", "Photosynthesis"]'
        topics, digest = ai_processor._parse_llm_topics(text)
        assert topics == ["Calculus Limits", "Photosynthesis"]
        assert "STUDY_TOPICS_JSON" not in digest

    def test_quoted_key_topics(self):
        text = '```json\n{"STUDY_TOPICS_JSON": ["Kinematics"]}\n```'
        topics, _ = ai_processor._parse_llm_topics(text)
        assert topics == ["Kinematics"]

    def test_both_markers_coexist(self):
        text = (
            "Digest text.\n"
            'STUDY_TOPICS_JSON:["Limits"]\n'
            'TASKS_JSON:[{"title":"Problem set"}]'
        )
        tasks, digest = ai_processor._parse_llm_tasks(text)
        topics, digest = ai_processor._parse_llm_topics(digest)
        assert [t["title"] for t in tasks] == ["Problem set"]
        assert topics == ["Limits"]
        assert "TASKS_JSON" not in digest
        assert "STUDY_TOPICS_JSON" not in digest
        assert "Digest text." in digest

    def test_single_topic_string(self):
        text = 'STUDY_TOPICS_JSON:"Thermodynamics"'
        topics, _ = ai_processor._parse_llm_topics(text)
        assert topics == []  # a bare string is not a JSON container; ignored safely


class TestDigestCleanup:
    def test_human_text_survives_payload_removal(self):
        text = (
            "⚡ **Needs attention**\n"
            "• Physics lab due Friday\n\n"
            'TASKS_JSON:[{"title":"Physics lab","due_date":"2026-09-04"}]'
        )
        tasks, digest = ai_processor._parse_llm_tasks(text)
        assert len(tasks) == 1
        assert "Needs attention" in digest
        assert "Physics lab due Friday" in digest
        assert "TASKS_JSON" not in digest
        assert "[{" not in digest

    def test_empty_fence_is_removed(self):
        text = '```json\n{"TASKS_JSON": [{"title":"T"}]}\n```'
        _, digest = ai_processor._parse_llm_tasks(text)
        assert "```" not in digest or digest.strip() == ""
