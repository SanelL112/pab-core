"""Small, privacy-safe routing state consumed by the status dashboard."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


def _state_file() -> Path:
    runtime = Path(os.getenv("PAB_RUNTIME_DIR", Path(__file__).resolve().parents[1]))
    return runtime / "dashboard_route_state.json"


def record_route(provider: str, mode: str, outcome: str = "success") -> None:
    """Persist only operational routing metadata, never prompts or responses."""
    target = _state_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"provider": provider, "mode": mode, "outcome": outcome, "updated_at": time.time()}
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".dashboard-route-", dir=target.parent)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except OSError:
        try:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
        except Exception:
            pass


def load_route() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {}
