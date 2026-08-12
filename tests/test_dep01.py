import sys
import os
import pytest
from unittest.mock import patch, MagicMock

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

def test_study_builder_consumes_only_validated_local_public_cache(tmp_path, monkeypatch):
    """Private guide builds must not issue search, image, or transcript requests."""
    import scrapers.mega_study_builder as builder

    monkeypatch.setattr(builder, "STUDY_DATABASE_DIR", tmp_path)
    (tmp_path / "test_topic.md").write_text("validated public context", encoding="utf-8")

    res_sources, res_text = builder.search_web_article("test topic")
    assert res_sources == [{"title": "test_topic", "href": "local-public-cache"}]
    assert res_text == "validated public context"
    assert builder.search_images("test topic") == []
    assert builder.search_youtube("test topic") == (None, "")

@patch("study_companion.YouTubeTranscriptApi")
def test_youtube_transcript_compatibility(m_ytt):
    from study_companion import get_transcript

    m_instance = m_ytt.return_value
    m_transcript = MagicMock()
    m_transcript.snippets = [MagicMock(text="hello"), MagicMock(text="world")]
    m_instance.fetch.return_value = m_transcript

    text = get_transcript("test_id")
    assert text == "hello world"
    m_instance.fetch.assert_called_once_with("test_id")
