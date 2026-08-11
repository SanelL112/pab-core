# Personal Assistant Bot - Repair Changes Summary

**Branch:** `fix/2026-07-bug-audit`
**Base Commit:** `4c81ad5` (main)
**Date:** 2026-07-23
**Original repair scope:** 29 files, 702 insertions(+), 603 deletions(-)

**Post-verification note (2026-07-23):** The active worktree contains the
original repair plus follow-up test, command-handler, and log-scanner fixes.
Generated user data and audit artifacts are intentionally excluded from the
repair commit.

---

## Overview

This document captures all agent-owned code changes from the bug audit repair work. User-generated files (activity_log.jsonl, curated_brain.md, mega_index.md, embedding_data/, caches) and audit reports are excluded.

---

## Security Fixes (P0 - Critical)

### 1. Command Execution Guard (`utils.py`)
- **Removed** `python3 -c` from `ALLOWED_COMMAND_TEMPLATES`
- **Removed** Python-specific blocklist validation (was insufficient)
- **Impact:** Arbitrary Python code execution via BASH tags is now blocked
- **Preserves:** Safe read-only shell commands (df, ps, cat, git log, etc.)

### 2. Telegram Authentication (`bot/security.py`, `main.py`)
- **Added** `@require_auth` decorator to `start()` handler
- **Fail-closed** on missing/unresolved `Update` objects (no bypass)
- **Scheduled jobs** now use `config.SANEL_CHAT_ID` exclusively, never caller chat ID
- **Handler registration** unchanged

### 3. Surface Cluster Manager API (`surface/cluster_manager.py`)
- **Default bind** changed from `0.0.0.0` → `127.0.0.1` (configurable via `CLUSTER_MANAGER_BIND_HOST`)
- **Bearer token auth** required for all `/api/*` endpoints (`CLUSTER_MANAGER_API_TOKEN`)
- **Timing-safe** token comparison via `secrets.compare_digest()`
- **Download validation:** rejects path traversal (`/`, `\`, `..`), enforces `https://huggingface.co/*/resolve/*` URLs only
- **UI unchanged:** root `/` remains unauthenticated, token input in sessionStorage only

---

## Privacy & Routing Fixes (P0/P1)

### 4. Private Workload Cloud Fail-Closed (`llm_router.py`, multiple callers)
- **New** `allow_cloud` parameter (default `True` for compat) on `call_local_rpc()`
- **Private callers** pass `allow_cloud=False`: watchdog, OCR/photo, curated-brain, PII route
- **Local fallback chain:** Surface → Pi Ollama → Dell local Ollama
- **Fail message:** `"⚠️ Local inference unavailable and cloud fallback disabled."` (no cloud call)

### 5. Dell Local Ollama Role (`config.py`, `llm_router.py`)
- **Added** `OLLAMA_LOCAL_URL` (default `http://127.0.0.1:11434`)
- **call_local_rpc()** now attempts Dell local as third tier before cloud

### 6. Cross-Provider Argument Order (`bot/ai_bridge.py`)
- **Fixed** `call_opencode(prompt=..., model=...)` and `call_hackclub(prompt=..., model=...)` — was reversed

### 7. Direct OpenRouter Bypass Removal (`bot/ai_bridge.py`)
- **Removed** `_call_or()` streaming HTTP client (direct `openrouter.ai` call)
- **Routes** through `llm_router.call_openrouter()` for unified PII scrub, fallback, cost tracking

### 8. PII Fail-Closed in Chat Bridge (`bot/ai_bridge.py`)
- **On Pi failure:** returns privacy-safe message, **does not** fall through to cloud with scrubbed text
- **System prompt** corrected: no more "root access" or `python3 -c` examples

---

## Reliability & Data Integrity Fixes (P1)

### 9. State Transaction Primitive (`bot/state.py`)
- **Added** `update_state(mutator)` — lock-held load/mutate/atomic-save
- **Used** by watchdog, digest, photo handlers for `seen_tasks`/`seen_alerts`

### 10. Notion Idempotency (`main.py`)
- **Tasks marked seen only after** `add_task_to_notion()` returns truthy page ID
- **Failure:** task NOT persisted to `seen_tasks`, NOT announced as success

### 11. Single Cache Root (`config.py`, `ai_processor.py`, `main.py`, scrapers)
- **Canonical:** `config.CACHE_DIR` (`cache/`)
- **Legacy paths** (`source_cache/`, `scrapers/source_cache/`) no longer written
- **Hash detection** uses full content (`data[:1000]` → `data`)

### 12. Nightly Queue Unification & Safe Acknowledgement (`config.py`, `main.py`, `scrapers/nightly_processor.py`)
- **Single path:** `config.NIGHTLY_QUEUE_FILE` (`nightly_queue.json`)
- **Atomic write** via temp file + `os.replace()`
- **Failed items retained** with `attempt_count`, `last_error`, `retryable` fields
- **No destructive clear** of entire queue

### 13. Nightly Entrypoint Cleanup (`nightly_processor.py`, `scrapers/nightly_processor.py`)
- **Removed** duplicate `__main__` blocks, `git pull/commit/push`, `pkill ollama serve`
- **Single** entry point; no production repo mutation

### 14. Async Cleanup Lifecycle (`bot/runtime.py`, `llm_router.py`, `utils.py`, `main.py`)
- **Replaced** `atexit`-only async client close with PTB `post_shutdown` hooks
- **New async functions:** `cleanup_background_tasks()`, `cleanup_llm_clients()`, `cleanup_async_caches()`
- **atexit** retains sync-only fallback

### 15. Media Handler Leak Fixes (`main.py`)
- **try/finally** cleanup for `.ogg` (voice) and `.jpg` (photo) temp files
- **Offloaded** blocking OCR/transcription to `asyncio.to_thread()`
- **User errors** generic (no exception internals leaked)

### 14. Logging Order & Non-Blocking Telegram Sink (`main.py`, `telegram_logger.py`)
- **`basicConfig()` before** `setup_telegram_logging()` → journald gets normal logs
- **TelegramHandler** now queue-backed with background worker thread
- **Cooldown/deduplication** by fingerprint (15s default)
- **Feedback-loop guard:** never re-sends Telegram API errors

### 15. Log Scanner Timestamp Fix (`log_scanner.py`)
- **Parses** numeric `ts` + `date`/`time` from `activity_log.jsonl`
- **Unparseable** → epoch 0 (not "now")

---

## Dependency & Low-Risk Fixes

### 16. Deprecated API Replacements
- **`duckduckgo_search` → `ddgs`** (in `scrapers/mega_study_builder.py`)
- **`YouTubeTranscriptApi.get_transcript()` → instance API** (in `study_companion.py`)

### 17. Requirements Cleanup
- **Removed** duplicate `httpx`
- **Pinned** all direct dependencies to installed versions
- **Created** constraints (implicit in requirements.txt)

### 18. UI Replacement Characters (`inline_keyboards.py`, `utils.py`, `practice_grader.py`, `main.py`)
- **U+FFFD (�)** replaced with appropriate emojis/plain text
- **Test:** `test_ui_emoji.py` scans for U+FFFD in tracked `.py` files

### 15. Syntax Warnings (`patch_utils.py`, `scripts/telegram_notify.py`)
- **Fixed** invalid escape sequences (`\s` → `\\s`, `\|` → `\\|`)

### 16. Import-Side-Effect Guards (`patch_utils.py`, `fix_utils_pii.py`, `fix_bot_commands.py`, `clean_emojis.py`, `send_telegram.py`)
- **Wrapped** top-level mutation in `if __name__ == "__main__":`
- **Test:** `test_script_imports.py` mocks file/net and asserts no mutation on import

---

## Test Coverage Added

| Test File | Scope |
|-----------|-------|
| `tests/test_p0_security.py` | Command guard, Telegram auth, Cluster API auth/download |
| `tests/test_routing_privacy.py` | Provider arg order, `allow_cloud=False` block, Dell fallback, no direct OpenRouter |
| `tests/test_reliability.py` | Cache paths, queue ack, state concurrency, Notion failure, full-content hash, nightly import safety |
| `tests/test_reliability_tranche.py` | Cache dir, queue failure handling, state primitive |
| `tests/test_telegram_logger.py` | Queue-backed emit, no synchronous network I/O |
| `tests/test_main_fixes.py` | Main.py specific behavior regression |
| `tests/test_dep01.py` | DDG/YouTube API compatibility |
| `tests/test_requirements.py` | No duplicate requirements |
| `tests/test_ui_emoji.py` | No U+FFFD in tracked Python |
| `tests/test_script_imports.py` | No import-time side effects |

The original repair suite reported 35 tests. After verification coverage was
added, **all 41 tests pass**. Full suite runs in ~25s.

---

## Files Modified (Agent-Owned Only)

```
ai_processor.py
bot/ai_bridge.py
bot/runtime.py
bot/security.py
bot/state.py
clean_emojis.py
config.py
fix_bot_commands.py
fix_utils_pii.py
inline_keyboards.py
llm_router.py
log_scanner.py
main.py
nightly_processor.py
patch_utils.py
practice_grader.py
requirements.txt
run_watchdog.py
scrapers/embedding_indexer.py
scrapers/mega_study_builder.py
scrapers/memory_consolidation.py
scrapers/nightly_processor.py
scrapers/offline_indexer.py
scripts/telegram_notify.py
send_telegram.py
study_companion.py
surface/cluster_manager.py
telegram_logger.py
utils.py
```

**Unchanged (user data/artifacts):** `activity_log.jsonl`, `curated_brain.md`, `mega_index.md`, `embedding_data/`, `cache/`, `source_cache/`, `scrapers/source_cache/`, `BUG_AUDIT_VERIFICATION_REPORT.md`, `CONSOLIDATED_BUG_AUDIT_AND_REMEDIATION.md`, `SECURITY_AUDIT_*.md`

---

## Verification Commands

```bash
# All tests
./venv/bin/pytest tests -q
# → 41 passed, 1 deprecation warning (PyPDF2)

# Syntax & compilation
python3 -W error::SyntaxWarning -m py_compile $(git ls-files '*.py')
# → passes

# Whitespace
git diff --check
# → clean

# No regressions in core behavior
git diff --stat
# → 29 files, 702+ / 603-
```

---

## Open Items

1. **Canvas token** expired (external; needs Composio re-auth)
2. **Runtime cluster participation** unproven (requires coordinated service restart)
3. **Service deployment** of changes (bot restart needed for config/env changes)
4. ~~**Health check script** (`scripts/bot-health-check.sh`) still reads stale `source_cache/` path~~ — resolved; it reads `cache/`.
5. ~~**Additional async-blocking call sites** in `scrapers/morning_digest.py` and `scripts/generate_daily_digest.py`~~ — resolved; their network calls are offloaded.
