"""Per-class topic discovery from the extraction caches."""
import json
from datetime import datetime, timedelta

from scrapers import topic_discovery as td


def _write_caches(tmp_path, canvas, onenote):
    (tmp_path / "canvas_page_extractions.json").write_text(json.dumps(canvas))
    (tmp_path / "onenote_page_extractions.json").write_text(json.dumps(onenote))


def test_discovery_groups_by_class_and_filters_junk(tmp_path, monkeypatch):
    _write_caches(
        tmp_path,
        canvas={"AP Biology - Bleier/Lab Safety": [
            {"title": "Lab Safety Quiz", "due_date": "2026-09-10"},
            {"title": "Advisement", "due_date": "2026-09-10"},
        ]},
        onenote={"AP Calculus AB 2026-2027/Bond 1st Period/Limits": [
            {"title": "Unit 1 Limits", "due_date": "2026-09-02"},
        ]},
    )
    monkeypatch.setattr(td, "_llm_refine_batch", lambda cm: {})
    result = td.discover_topics_per_class(cache_dir=tmp_path, use_online_refine=False)

    assert set(result) == {"AP Biology - Bleier", "AP Calculus AB 2026-2027"}
    bio_topics = " ".join(result["AP Biology - Bleier"]).lower()
    assert "lab safety" in bio_topics
    assert "advisement" not in bio_topics  # junk filtered
    assert any("limits" in t.lower() for t in result["AP Calculus AB 2026-2027"])


def test_empty_material_class_is_dropped(tmp_path, monkeypatch):
    _write_caches(tmp_path, canvas={"AP Stat 25-26/x": []}, onenote={})
    monkeypatch.setattr(td, "_llm_refine_batch", lambda cm: {})
    result = td.discover_topics_per_class(cache_dir=tmp_path, use_online_refine=False)
    assert result == {}


def test_online_refine_results_preferred(tmp_path, monkeypatch):
    _write_caches(
        tmp_path,
        canvas={"AP Calc/Limits": [{"title": "Worksheet 1", "due_date": "2026-09-01"}]},
        onenote={},
    )
    monkeypatch.setattr(
        td, "_llm_refine_batch",
        lambda cm: {"AP Calc": ["Limits and Continuity"]}
    )
    result = td.discover_topics_per_class(cache_dir=tmp_path, use_online_refine=True)
    assert result["AP Calc"][0] == "Limits and Continuity"


def test_past_date_only_titles_are_dropped(tmp_path, monkeypatch):
    """Calendar/agenda pages titled with a past date must not become topics."""
    future = (datetime.now() + timedelta(days=10)).strftime("%Y-%m-%d")
    _write_caches(
        tmp_path,
        canvas={
            "AP Biology - Bleier/Lab Safety": [
                {"title": "Lab Safety Quiz", "due_date": future},
            ],
            # Pure past-date page titles (the bug): dropped.
            "AP Biology - Bleier/Aug 24": [{"title": "Aug 24", "due_date": future}],
            "AP Biology - Bleier/2026-08-24": [{"title": "2026-08-24", "due_date": future}],
        },
        onenote={},
    )
    monkeypatch.setattr(td, "_llm_refine_batch", lambda cm: {})
    result = td.discover_topics_per_class(cache_dir=tmp_path, use_online_refine=False)
    topics = " ".join(result.get("AP Biology - Bleier", [])).lower()
    assert "lab safety" in topics
    assert "aug 24" not in topics
    assert "2026-08-24" not in topics


def _dated_caches(days_offsets: dict[str, int]):
    """cache dict: class name -> list of pages, one dated task each."""
    from datetime import datetime, timedelta

    canvas = {}
    for cls, offset in days_offsets.items():
        due = (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")
        canvas[f"{cls}/Page One"] = [{"title": f"{cls} First Topic", "due_date": due}]
        canvas[f"{cls}/Page Two"] = [{"title": f"{cls} Second Topic", "due_date": due}]
    return canvas


def test_cap_round_robin_one_topic_per_class(tmp_path, monkeypatch):
    """Cap below class count: each class keeps ONE topic, none get a second."""
    _write_caches(
        tmp_path,
        canvas=_dated_caches({"Alpha": 5, "Beta": 10, "Gamma": 20}),
        onenote={},
    )
    monkeypatch.setattr(td, "_llm_refine_batch", lambda cm: {})
    result = td.discover_topics_per_class(
        cache_dir=tmp_path, use_online_refine=False, max_total_topics=3
    )
    assert set(result) == {"Alpha", "Beta", "Gamma"}
    for topics in result.values():
        assert len(topics) == 1
    assert result["Alpha"][0] == "Page One"


def test_cap_prefers_soonest_due_classes(tmp_path, monkeypatch):
    """Budget smaller than class count: the soonest-due classes win the seats."""
    _write_caches(
        tmp_path,
        canvas=_dated_caches({"Early": 3, "Mid": 15, "Late": 45}),
        onenote={},
    )
    monkeypatch.setattr(td, "_llm_refine_batch", lambda cm: {})
    result = td.discover_topics_per_class(
        cache_dir=tmp_path, use_online_refine=False, max_total_topics=2
    )
    assert set(result) == {"Early", "Mid"}  # Late (45d) dropped
    assert result["Early"] == ["Page One"]
    assert result["Mid"] == ["Page One"]


def test_cap_deepens_highest_priority_class_first(tmp_path, monkeypatch):
    """Budget above class count: leftover seats deepen in priority order."""
    _write_caches(
        tmp_path,
        canvas=_dated_caches({"Early": 3, "Mid": 15}),
        onenote={},
    )
    monkeypatch.setattr(td, "_llm_refine_batch", lambda cm: {})
    result = td.discover_topics_per_class(
        cache_dir=tmp_path, use_online_refine=False, max_total_topics=3
    )
    assert set(result) == {"Early", "Mid"}
    assert result["Early"] == ["Page One", "Page Two"]
    assert result["Mid"] == ["Page One"]
