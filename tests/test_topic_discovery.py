"""Per-class topic discovery from the extraction caches."""
import json

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
