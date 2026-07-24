"""Regression coverage for study-guide privacy boundaries."""

from unittest.mock import patch


def test_chunked_study_builder_uses_local_model_for_private_context():
    from scrapers import mega_study_builder

    with patch.object(mega_study_builder, "call_agy", return_value="local guide") as call_local:
        result = mega_study_builder.call_private_guide_model("private classroom notes", timeout=42)

    assert result == "local guide"
    call_local.assert_called_once_with("private classroom notes", timeout=42, model="flash")


def test_scheduled_mega_guide_fails_closed_when_local_model_is_unavailable():
    import generate_mega_guide

    with patch.object(generate_mega_guide, "call_agy", return_value="") as call_local:
        result = generate_mega_guide.call_private_guide_model("private cached summaries", timeout=42)

    assert "not sent to cloud" in result.lower()
    call_local.assert_called_once_with("private cached summaries", timeout=42, model="flash")
