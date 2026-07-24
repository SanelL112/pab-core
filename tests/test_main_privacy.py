import inspect
import stat

import main


def test_photo_extract_storage_is_filtered_and_private(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "BOT_DIR", str(tmp_path))

    main._append_important_extract("Homework: complete problems 1-5")

    extract_path = tmp_path / "important_extracts.txt"
    contents = extract_path.read_text()
    assert "Filtered Extract" in contents
    assert "Raw OCR" not in contents
    assert stat.S_IMODE(extract_path.stat().st_mode) == 0o600


def test_photo_handler_does_not_persist_raw_ocr_to_history():
    source = inspect.getsource(main.handle_photo)

    assert "Photo Upload (Raw OCR)" not in source
    assert "persist_history=False" in source
