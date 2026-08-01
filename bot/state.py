"""Transactional state management for the personal assistant bot."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from weakref import WeakValueDictionary

import config
from bot.storage import AtomicJSONStore

logger = logging.getLogger(__name__)

# Weak values prevent one lock entry per historical chat from accumulating for
# the lifetime of the process.  The guard covers concurrent lock creation.
_user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
_user_locks_guard = threading.Lock()


def _default_state() -> dict:
    return {
        "seen_tasks": [],
        "seen_alerts": [],
        "pending_priorities": {},
        "pending_tasks": {},
        "user_models": {},
    }


# ``seen_tasks`` is fuzzy-matched in full against every candidate task on each
# digest, so an unbounded list costs O(n) comparisons and — worse — steadily
# raises the chance a legitimate new task collides with an ancient title above
# the 0.8 similarity threshold and is silently dropped.  Keep a bounded, recent
# window instead.  Legacy MD5/SHA digests can never fuzzy-match a real title,
# so they are pure dead weight and are discarded.
MAX_SEEN_TASKS = 300
MAX_SEEN_ALERTS = 300
_LEGACY_DIGEST = re.compile(r"\A[0-9a-f]{32}(?:[0-9a-f]{32})?\Z")


def _prune_seen_list(values: list, limit: int, *, drop_digests: bool) -> list:
    """Return the most recent ``limit`` usable entries, oldest first."""
    cleaned = []
    for value in values:
        if not isinstance(value, str):
            continue
        entry = value.strip()
        if not entry:
            continue
        if drop_digests and _LEGACY_DIGEST.match(entry):
            continue
        cleaned.append(entry)
    return cleaned[-limit:]


def _normalize_state(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("state root must be a JSON object")
    state = _default_state()
    state.update(value)
    for key in ("seen_tasks", "seen_alerts"):
        if not isinstance(state[key], list):
            state[key] = []
    state["seen_tasks"] = _prune_seen_list(
        state["seen_tasks"], MAX_SEEN_TASKS, drop_digests=True
    )
    # Alert keys are intentionally hashes, so only bound their growth.
    state["seen_alerts"] = _prune_seen_list(
        state["seen_alerts"], MAX_SEEN_ALERTS, drop_digests=False
    )
    for key in ("pending_priorities", "pending_tasks", "user_models"):
        if not isinstance(state[key], dict):
            state[key] = {}
    return state


def _store() -> AtomicJSONStore[dict]:
    # Resolve dynamically so test/runtime configuration can safely redirect
    # state without re-importing this module.
    return AtomicJSONStore(config.STATE_FILE, _default_state)

def get_user_lock(chat_id: int) -> asyncio.Lock:
    with _user_locks_guard:
        lock = _user_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[chat_id] = lock
        return lock


def get_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_sleep_window() -> bool:
    """Returns True if current time is between 1 AM and 7 AM Eastern Time."""
    try:
        import datetime
        from zoneinfo import ZoneInfo
        et_tz = ZoneInfo('America/New_York')
        now_et = datetime.datetime.now(datetime.timezone.utc).astimezone(et_tz)
        return 1 <= now_et.hour < 7
    except Exception:
        return False


def load_state() -> dict:
    """Load a normalized state snapshot under a cross-process read lock."""
    return _normalize_state(_store().read())


def save_state(state: dict) -> None:
    """Durably replace the state with a normalized snapshot."""
    _store().write(_normalize_state(state))

def update_state(mutator) -> dict:
    """Atomically read, mutate, normalize, and commit state."""
    def apply(state: dict) -> dict:
        normalized = _normalize_state(state)
        replacement = mutator(normalized)
        if replacement is not None:
            normalized = _normalize_state(replacement)
        return _normalize_state(normalized)

    return _store().update(apply)
