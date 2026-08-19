import pytest
from pathlib import Path
from scrapers.lfm_vision_harness import (
    extract_external_platform_links,
    fetch_google_doc_or_slides_text,
    process_and_save_external_resource,
)


def test_extract_external_platform_links():
    html_doc = """
    <div>
        <p>Please review today's lecture:</p>
        <iframe src="https://docs.google.com/presentation/d/123456789/embed" title="Unit 3 AP Physics Slides"></iframe>
        <a href="https://docs.google.com/document/d/987654321/edit">Chapter 4 Reading Notes</a>
        <a href="https://forms.gle/abcdef123">Exit Ticket Form</a>
        <a href="https://www.deltamath.com/student">DeltaMath Assignment</a>
    </div>
    """
    links = extract_external_platform_links(html_doc)
    types = [item["type"] for item in links]
    assert "iframe" in types
    assert "Google Doc" in types
    assert "Google Form" in types
    assert "Educational Platform" in types
    assert any("Unit 3 AP Physics Slides" in item["title"] for item in links)


def test_save_external_resource(tmp_path, monkeypatch):
    import scrapers.lfm_vision_harness as harness
    monkeypatch.setattr(harness, "ACADEMIC_NOTES_DIR", tmp_path)

    saved = process_and_save_external_resource(
        course_name="AP Physics C",
        resource_title="Rotational Dynamics Lecture Notes",
        resource_url="https://docs.google.com/presentation/d/12345/edit",
        raw_content="Torque tau = I * alpha. Moment of inertia for a disk is 1/2 M R^2.",
    )
    assert saved.exists()
    content = saved.read_text(encoding="utf-8")
    assert "Rotational Dynamics" in content
    assert "Torque tau" in content
