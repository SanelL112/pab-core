"""Regression coverage for study-guide cloud-consent boundaries."""

from unittest.mock import patch


def test_chunked_study_builder_defaults_to_local_model():
    from scrapers import mega_study_builder

    with patch.object(mega_study_builder, "call_agy", return_value="local guide") as call_local:
        result = mega_study_builder.call_guide_model("private classroom notes", timeout=42)

    assert result == "local guide"
    call_local.assert_called_once_with("private classroom notes", timeout=42, model="flash")


def test_chunked_study_builder_uses_cloud_only_after_explicit_public_opt_in():
    from scrapers import mega_study_builder

    with patch.object(mega_study_builder, "MEGA_GUIDE_CLOUD_CLASSIFICATION", "PUBLIC"), \
         patch.object(mega_study_builder, "OPENROUTER_API_KEY", "test-key"), \
         patch("llm_router.call_openrouter", return_value="cloud guide") as call_cloud, \
         patch.object(mega_study_builder, "call_agy") as call_local:
        result = mega_study_builder.call_guide_model("approved classroom notes", timeout=42)

    assert result == "cloud guide"
    assert call_cloud.call_args.kwargs["classification"] == "PUBLIC"
    call_local.assert_not_called()


def test_scheduled_mega_guide_keeps_local_fallback_when_cloud_is_not_opted_in():
    import generate_mega_guide

    with patch.object(generate_mega_guide, "call_agy", return_value="") as call_local:
        result = generate_mega_guide.call_guide_model("private cached summaries", timeout=42)

    assert "not sent to cloud" in result.lower()
    call_local.assert_called_once_with("private cached summaries", timeout=42, model="flash")


def test_scheduled_mega_guide_uses_cloud_after_explicit_public_opt_in():
    import generate_mega_guide

    with patch.object(generate_mega_guide, "MEGA_GUIDE_CLOUD_CLASSIFICATION", "PUBLIC"), \
         patch.object(generate_mega_guide, "OPENROUTER_API_KEY", "test-key"), \
         patch("llm_router.call_openrouter", return_value="cloud mega guide") as call_cloud, \
         patch.object(generate_mega_guide, "call_agy") as call_local:
        result = generate_mega_guide.call_guide_model("approved cached summaries", timeout=42)

    assert result == "cloud mega guide"
    assert call_cloud.call_args.kwargs["classification"] == "PUBLIC"
    call_local.assert_not_called()
