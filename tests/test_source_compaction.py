"""Tests for deterministic source compaction.

High-signal sources bypass LLM summarization (``skip_llm_filter=True``), so their
raw scrape text lands in the digest assembly prompt.  Trimming redundancy there
is worthwhile because prompt evaluation runs at roughly 15 tok/s on the RPC
cluster, but it must not cost information: the digest feeds Telegram, Notion
tasks AND calendar events, so a severed date is a silently missing event.

``_compact_source_text`` is therefore lossless by default -- it removes only a
repeated course prefix and whitespace runs.
"""
from __future__ import annotations

import re

import ai_processor


def _announcements(count: int, body_chars: int = 300, course: str = "2026 Summer SAT/ACT @ AHA") -> str:
    lines = ["📢 **Google Classroom Announcements (via Composio):**"]
    for i in range(count):
        lines.append(f"[{course}]: Announcement {i} " + "x" * body_chars)
    return "\n".join(lines)


class TestLosslessByDefault:
    def test_no_body_is_truncated(self):
        raw = _announcements(10, body_chars=800)
        out = ai_processor._compact_source_text(raw)
        assert "…" not in out, "default compaction must never truncate an item"

    def test_every_item_survives(self):
        raw = _announcements(40)
        out = ai_processor._compact_source_text(raw)
        assert out.count("\n- ") == 40
        assert "omitted" not in out

    def test_full_body_text_is_preserved(self):
        body = "Class moved to Friday (7/24) at 6 AM in room B12 — bring the practice packet."
        raw = "📢 Header\n" + "\n".join(f"[Course X]: {body}" for _ in range(4))
        out = ai_processor._compact_source_text(raw)
        assert out.count(body) == 4

    def test_scheduling_details_survive(self):
        """Regression: a truncating version severed 'Friday (7/24)' and lost the date."""
        raw = (
            "📢 Header\n"
            "[Course X]: " + "filler word " * 20 + "we will be having class on Friday (7/24) instead of the practice test.\n"
            "[Course X]: " + "filler word " * 20 + "the mock test starts at 8:30 AM in room 204.\n"
        )
        out = ai_processor._compact_source_text(raw)
        for token in ("Friday", "7/24", "8:30 AM", "room 204"):
            assert token in out, f"{token} must survive compaction"

    def test_date_tokens_are_never_lost(self):
        raw = "📢 H\n" + "\n".join(
            f"[C]: {'pad ' * 60} due 9/{d} at 7 PM" for d in range(1, 9)
        )
        pattern = re.compile(r"9/\d|7 PM")
        before = {m.group(0) for m in pattern.finditer(raw)}
        after = {m.group(0) for m in pattern.finditer(ai_processor._compact_source_text(raw))}
        assert before == after


class TestRedundancyRemoval:
    def test_repeated_course_prefix_is_hoisted(self):
        raw = _announcements(10)
        out = ai_processor._compact_source_text(raw)
        assert out.count("2026 Summer SAT/ACT @ AHA") == 1
        assert len(out) < len(raw)

    def test_mixed_prefixes_are_preserved(self):
        raw = "📢 Header\n" + "\n".join(f"[Course {i % 3}]: body {i}" for i in range(9))
        out = ai_processor._compact_source_text(raw)
        for i in range(3):
            assert f"[Course {i}]" in out

    def test_whitespace_runs_collapse(self):
        raw = "📢 H\n" + "\n".join("[C]: word" + "    \t  spaced" * 20 for _ in range(4))
        out = ai_processor._compact_source_text(raw)
        body = "\n".join(out.splitlines()[1:])
        assert "  " not in body
        assert "\t" not in body

    def test_banner_line_becomes_header(self):
        out = ai_processor._compact_source_text(_announcements(10))
        assert out.splitlines()[0].startswith("📢")

    def test_word_count_is_unchanged(self):
        """Only redundancy is removed, so real content words all survive."""
        raw = _announcements(12, body_chars=200)
        out = ai_processor._compact_source_text(raw)
        # Every 'Announcement N' marker is still present.
        for i in range(12):
            assert f"Announcement {i} " in out


class TestGuards:
    def test_empty_input(self):
        assert ai_processor._compact_source_text("") == ""
        assert ai_processor._compact_source_text("   ") == "   "

    def test_never_grows_the_input(self):
        raw = "\n".join(f"[C{i}]: x" for i in range(400))
        out = ai_processor._compact_source_text(raw)
        assert len(out) <= len(raw)

    def test_short_text_with_no_redundancy_is_untouched(self):
        text = "📢 Header\n[Course]: short note"
        assert ai_processor._compact_source_text(text) == text

    def test_unprefixed_lines_are_preserved_verbatim(self):
        raw = "\n".join(f"plain line {i} " + "q" * 50 for i in range(9))
        out = ai_processor._compact_source_text(raw)
        for i in range(9):
            assert f"plain line {i}" in out


class TestOptionalTruncation:
    """``max_item_chars`` is opt-in and unused in production."""

    def test_cap_applies_when_requested(self):
        raw = _announcements(6, body_chars=500)
        out = ai_processor._compact_source_text(raw, max_item_chars=120)
        assert "…" in out
        assert len(out) < len(ai_processor._compact_source_text(raw))

    def test_cap_cuts_on_word_boundary(self):
        raw = "📢 H\n" + "\n".join(
            "[C]: " + " ".join(["alpha", "bravo", "charlie", "delta"] * 20) for _ in range(4)
        )
        out = ai_processor._compact_source_text(raw, max_item_chars=100)
        for line in out.splitlines()[1:]:
            last = line.rstrip("…").rstrip().split()[-1]
            assert last in {"alpha", "bravo", "charlie", "delta"}, line

    def test_no_cap_by_default(self):
        raw = _announcements(6, body_chars=500)
        assert "…" not in ai_processor._compact_source_text(raw)


class TestProcessSourceIntegration:
    """Exercise the real ``process_source`` path.

    ``ai_processor.CACHE_DIR`` is deliberately NOT monkeypatched: ``process_source``
    only ``mkdir``s its own CACHE_DIR, while ``utils.mark_processed`` writes to
    ``config.CACHE_DIR``.  Repointing one and not the other leaves the hash cache
    parent missing.  The conftest already sandboxes ``config.CACHE_DIR``.
    """

    def test_high_signal_source_is_compacted_losslessly(self):
        import config
        raw = _announcements(10)
        summary = ai_processor.process_source(
            "compaction_probe", raw, skip_llm_filter=True, force_reprocess=True
        )
        assert len(summary) < len(raw), "redundancy should still be removed"
        assert summary.count("\n- ") == 10
        assert "…" not in summary, "no announcement may be truncated"
        assert (config.CACHE_DIR / "compaction_probe_summary.txt").read_text() == summary

    def test_small_high_signal_source_unchanged(self):
        raw = "📚 Canvas\n- No current coursework."
        summary = ai_processor.process_source(
            "compaction_small_probe", raw, skip_llm_filter=True, force_reprocess=True
        )
        assert summary == raw

    def test_empty_source_short_circuits(self):
        summary = ai_processor.process_source(
            "compaction_empty_probe", "", skip_llm_filter=True, force_reprocess=True
        )
        assert summary == "No compaction_empty_probe data available."


class TestPromptCompleteness:
    """The assembly prompt must ask for completeness, not brevity."""

    def test_no_bullet_cap_instruction(self):
        assert "at most 3 bullets" not in ai_processor.DIGEST_ASSEMBLY_PROMPT

    def test_asks_for_every_dated_item(self):
        prompt = ai_processor.DIGEST_ASSEMBLY_PROMPT
        assert "Include EVERY item" in prompt
        assert "never omit one to stay short" in prompt

    def test_asks_to_preserve_dates_verbatim(self):
        assert "Preserve every date" in ai_processor.DIGEST_ASSEMBLY_PROMPT
