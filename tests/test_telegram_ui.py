import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from bot import state
import config
from bot.ui import (
    HELP_TEXT,
    SAFE_MESSAGE_LENGTH,
    confirmation_keyboard,
    help_keyboard,
    home_keyboard,
    paginate_text,
    render_assistant_text,
    section_keyboard,
)
from inline_keyboards import (
    get_digest_topic_keyboard,
    get_new_tasks_keyboard,
    get_study_guide_keyboard,
    get_task_actions_keyboard,
)


def _callback_data(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def test_html_renderer_escapes_model_markup_and_keeps_safe_structure():
    rendered = render_assistant_text("# Plan\n- **Review** <script>alert(1)</script>\n`safe_code`")

    assert "<b>Plan</b>" in rendered
    assert "• <b>Review</b> &lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "`safe_code`" in rendered
    assert "<script>" not in rendered


def test_pagination_keeps_large_code_blocks_renderable():
    text = "Before\n\n```\n" + ("x" * (SAFE_MESSAGE_LENGTH + 250)) + "\n```\n\nAfter"
    chunks = paginate_text(text)

    assert len(chunks) >= 2
    assert all(len(chunk) <= SAFE_MESSAGE_LENGTH for chunk in chunks)
    code_chunks = [chunk for chunk in chunks if chunk.startswith("```")]
    assert code_chunks
    assert all(render_assistant_text(chunk).count("<pre>") == 1 for chunk in code_chunks)


def test_dashboard_and_help_callbacks_use_short_stable_ids():
    for markup in (home_keyboard(), help_keyboard(), section_keyboard("calendar"), confirmation_keyboard("cal:sync")):
        data = _callback_data(markup)
        assert data
        assert all(len(item.encode("utf-8")) <= 64 for item in data)

    assert "daily" in HELP_TEXT


def test_dynamic_legacy_keyboard_callback_data_never_exceeds_telegram_limit():
    long_value = "📚 " + ("very-long-topic-" * 20)
    keyboards = (
        get_digest_topic_keyboard([long_value]),
        get_new_tasks_keyboard([long_value]),
        get_task_actions_keyboard(long_value),
        get_study_guide_keyboard(long_value),
    )

    for markup in keyboards:
        assert all(len(item.encode("utf-8")) <= 64 for item in _callback_data(markup))


def test_state_save_accepts_legacy_string_paths(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(config, "STATE_FILE", path)
    monkeypatch.setattr(state, "STATE_FILE", path, raising=False)

    state.save_state({"seen_tasks": ["task"]})

    assert Path(path).exists()
    assert state.load_state()["seen_tasks"] == ["task"]
