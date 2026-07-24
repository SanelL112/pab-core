import os
import time

import utils


def test_chat_history_retention_deletes_only_expired_files_and_keeps_recent_files(tmp_path, monkeypatch):
    expired = tmp_path / "chat_history_1_old.txt"
    recent = tmp_path / "chat_history_1_current.txt"
    expired.write_text("old conversation")
    recent.write_text("current conversation")
    now = time.time()
    os.utime(expired, (now - 31 * 24 * 60 * 60, now - 31 * 24 * 60 * 60))

    monkeypatch.setattr(utils, "BASE_DIR", tmp_path)
    monkeypatch.setattr(utils, "CHAT_HISTORY_RETENTION_DAYS", 30)

    deleted = utils.prune_expired_chat_histories(now=now)

    assert deleted == 1
    assert not expired.exists()
    assert recent.exists()
