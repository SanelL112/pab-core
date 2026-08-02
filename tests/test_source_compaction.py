"""Tests for deterministic source compaction.

High-signal sources bypass LLM summarization (``skip_llm_filter=True``), so their
raw scrape text used to land in the digest assembly prompt verbatim.  Classroom
announcements alone were 54% of a 6,046-char prompt, and prompt evaluation runs
at roughly 15 tok/s on the RPC cluster, so ~100s of every digest call was spent
reading text nobody trimmed.

``_compact_source_text`` shrinks that deterministically -- no inference, so a
model timeout or refusal can never lose a due date.
"""
from __future__ import annotations

import ai_processor


def _announcements(count: int, body_chars: int = 300, course: str = "2026 Summer SAT/ACT @ AHA") -> str:
    lines = ["📢 **Google Classroom Announcements (via Composio):**"]
    for i in range(count):
        lines.append(f"[{course}]: Announcement {i} " + "x" * body_chars)
    return "\n".join(lines)


class TestPassthrough:
    def test_short_text_is_untouched(self):
        text = "📢 Header\n[Course]: short note"
        assert ai_processor._compact_source_text(text) == text

    def test_empty_input(self):
        assert ai_processor._compact_source_text("") == ""
        assert ai_processor._compact_source_text("   ") == "   "

    def test_never_grows_the_input(self):
        # Pathological shape: many tiny items whose bullets could add overhead.
        text = "\n".join(f"[C{i}]: x" for i in range(400))
        out = ai_processor._compact_source_text(text)
        assert len(out) <= len(text)

    def test_result_respects_total_budget(self):
        out = ai_processor._compact_source_text(_announcements(40), total_chars=2_000)
        assert len(out) <= 2_000


class TestPrefixHoisting:
    def test_repeated_course_prefix_is_hoisted(self):
        raw = _announcements(10)
        out = ai_processor._compact_source_text(raw)
        # The course name appears once in the header, not on all ten lines.
        assert out.count("2026 Summer SAT/ACT @ AHA") == 1
        assert len(out) < len(raw)

    def test_mixed_prefixes_are_preserved(self):
        raw = (
            "📢 Header\n"
            + "\n".join(f"[Course {i % 3}]: " + "y" * 300 for i in range(9))
        )
        out = ai_processor._compact_source_text(raw)
        # Attribution must survive when items come from different courses.
        for i in range(3):
            assert f"[Course {i}]" in out

    def test_banner_line_becomes_header(self):
        out = ai_processor._compact_source_text(_announcements(10))
        assert out.splitlines()[0].startswith("📢")


class TestItemPreservation:
    def test_all_items_survive_at_default_size(self):
        """Regression: the first implementation dropped the tail entirely."""
        raw = _announcements(10)
        out = ai_processor._compact_source_text(raw)
        assert out.count("\n- ") == 10
        assert "omitted" not in out

    def test_newest_items_are_kept_when_dropping_is_unavoidable(self):
        # Scrapers emit oldest-first, so the LAST item is the most recent.
        raw = _announcements(60, body_chars=400)
        out = ai_processor._compact_source_text(raw, total_chars=1_000)
        assert "Announcement 59" in out, "most recent announcement must survive"
        assert "omitted" in out

    def test_omission_note_counts_dropped_items(self):
        out = ai_processor._compact_source_text(_announcements(60, 400), total_chars=1_000)
        assert "older announcement(s) omitted" in out

    def test_per_item_budget_tightens_before_dropping(self):
        raw = _announcements(12, body_chars=400)
        out = ai_processor._compact_source_text(raw, total_chars=2_000)
        # Every item still present, just shorter.
        assert out.count("\n- ") == 12
        assert "omitted" not in out


class TestTextNormalization:
    def test_whitespace_runs_collapse(self):
        raw = "📢 H\n" + "\n".join(
            "[C]: word" + "    \t  spaced" * 40 for _ in range(6)
        )
        out = ai_processor._compact_source_text(raw)
        body = "\n".join(out.splitlines()[1:])
        assert "  " not in body
        assert "\t" not in body

    def test_greeting_boilerplate_is_trimmed(self):
        raw = "📢 H\n" + "\n".join(
            f"[C]: Hello all, real content {i} " + "z" * 300 for i in range(8)
        )
        out = ai_processor._compact_source_text(raw)
        assert "Hello all," not in out
        assert "real content" in out

    def test_truncation_marks_with_ellipsis(self):
        out = ai_processor._compact_source_text(_announcements(10, body_chars=500))
        assert "…" in out

    def test_truncation_avoids_mid_word_cuts(self):
        raw = "📢 H\n" + "\n".join(
            "[C]: " + " ".join(["alpha", "bravo", "charlie", "delta"] * 40) for _ in range(6)
        )
        out = ai_processor._compact_source_text(raw)
        for line in out.splitlines()[1:]:
            body = line.rstrip("…").rstrip()
            if body.endswith(("alpha", "bravo", "charlie", "delta")) or not body:
                continue
            # A partial word like "brav" should not be the final token.
            assert body.split()[-1] in {"alpha", "bravo", "charlie", "delta"}, line


class TestRealWorldShape:
    def test_actual_classroom_payload_shrinks_by_half(self):
        """Mirrors the live cache entry: 11 lines, one repeated course prefix."""
        raw = _announcements(10, body_chars=290)
        out = ai_processor._compact_source_text(raw)
        assert len(out) < len(raw) * 0.7
        assert out.count("\n- ") == 10

    def test_unprefixed_lines_still_compact(self):
        raw = "\n".join("plain line " + "q" * 300 for _ in range(9))
        out = ai_processor._compact_source_text(raw)
        assert len(out) < len(raw)


class TestProcessSourceIntegration:
    """Exercise the real ``process_source`` path.

    ``ai_processor.CACHE_DIR`` is deliberately NOT monkeypatched: ``process_source``
    only ``mkdir``s its own CACHE_DIR, while ``utils.mark_processed`` writes to
    ``config.CACHE_DIR``.  Repointing one and not the other leaves the hash cache
    parent missing.  The conftest already sandboxes ``config.CACHE_DIR`` under a
    temp runtime root, so use it directly with unique source names.
    """

    def test_high_signal_source_is_compacted_not_pasted(self):
        import config
        raw = _announcements(10)
        summary = ai_processor.process_source(
            "compaction_probe", raw, skip_llm_filter=True, force_reprocess=True
        )
        assert len(summary) < len(raw), "skip_llm_filter must no longer pass raw text through"
        assert summary.count("\n- ") == 10
        cached = config.CACHE_DIR / "compaction_probe_summary.txt"
        assert cached.read_text() == summary

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
