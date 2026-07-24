import sys
import os
import pytest
from unittest.mock import patch, MagicMock

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

@patch("scrapers.mega_study_builder.requests.get")
@patch("scrapers.mega_study_builder.DDGS")
def test_ddgs_compatibility(m_ddgs, m_req_get):
    from scrapers.mega_study_builder import search_web_article, search_images

    m_instance = m_ddgs.return_value
    m_instance.text.return_value = [{"title": "Test Title", "href": "http://test", "body": "test body"}]
    m_instance.images.return_value = [{"title": "Test Image", "image": "http://test.jpg"}]

    m_req_instance = MagicMock()
    m_req_instance.content = b"<html><body><p>test body</p></body></html>"
    m_req_get.return_value = m_req_instance

    res_sources, res_text = search_web_article("test topic")
    assert m_instance.text.call_count == 5
    assert "Test Title" in res_sources[0]["title"]
    assert "test body" in res_text

    res_img = search_images("test topic")
    assert res_img == [{"title": "Test Image", "image": "http://test.jpg"}]

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


@patch("scrapers.mega_study_builder.YouTubeTranscriptApi")
@patch("scrapers.mega_study_builder.VideosSearch")
def test_mega_youtube_search_handles_missing_titles_and_uses_instance_api(m_search, m_ytt):
    from scrapers.mega_study_builder import search_youtube

    m_search.return_value.result.side_effect = [
        {"result": [{"id": "video-id", "title": None}]},
        {"result": []},
    ]
    m_ytt.return_value.fetch.return_value.snippets = [MagicMock(text="transcript text")]

    metadata, text = search_youtube("Algebra")

    assert metadata["title"] == "Fetched 1 YouTube Transcripts | Untitled Video"
    assert "transcript text" in text
    m_ytt.return_value.fetch.assert_called_once_with("video-id")
