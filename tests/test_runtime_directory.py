"""Release checkout configuration tests."""

import os
import subprocess
import sys


def test_config_uses_explicit_runtime_directory_for_env_and_data(tmp_path):
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / ".env").write_text(
        "OPENROUTER_API_KEY=test-key\n"
        "TELEGRAM_BOT_TOKEN=test-token\n"
        "TELEGRAM_CHAT_ID=1\n"
        "CONVERSATION_ID=test-conversation\n"
    )
    env = os.environ | {"PAB_RUNTIME_DIR": str(runtime_dir)}

    result = subprocess.run(
        [sys.executable, "-c", "import config; print(config.BASE_DIR)"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.stdout.strip() == str(runtime_dir)
