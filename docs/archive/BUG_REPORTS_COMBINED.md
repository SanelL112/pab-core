# COMBINED BUG REPORTS - Personal Assistant Bot

## Source 1: AGY (Gemini 3.1 Pro) - First Audit
[See comprehensive_review.md at /home/sanel/.gemini/antigravity-cli/brain/f15c9c10-61de-4fcf-a1ad-0f77f6ab765b/comprehensive_review.md]

### CRITICAL (4 bugs)
1. **Contradictory Security Filters Break Core Features** - `bot/ai_bridge.py:149-166` vs `utils.py:124, 246-247`
   - System prompt tells LLM to use `subprocess` and `open()`, but `_is_command_allowed` blocks them
   - Fix: Remove `'open('` and `'subprocess'` from blocklist, or provide dedicated safe tools

2. **Async Event Loop Blocked by Subprocesses and I/O** - `bot/ai_bridge.py:575-576`, `bot/ai_bridge.py:192-194`, `llm_router.py:384-394`
   - `run_bash_safely` called synchronously in async function via `re.sub()`
   - `client.post()` blocks thread instead of streaming
   - Fix: Use `asyncio.to_thread` for blocking calls; use `client.stream()` for HTTP

3. **Config Drift & Broken File Paths** - `bot/ai_bridge.py:68-75, 129-130` vs `config.py:60-64`
   - `ai_bridge.py` uses `__file__` to find files in `bot/` dir, but config manages them in `BASE_DIR`
   - Fix: Import `LATEST_DIGEST_FILE`, `BOT_CONTEXT_FILE`, `BASE_DIR` from `config.py`

4. **Oscillating Digest Deduplication** - `ai_processor.py:352-394`
   - Overwrites `latest_digest.txt` with only new bullets → forgets old assignments → re-notifies them
   - Fix: Maintain two files: `seen_bullets.json` (rolling hash set) + `latest_digest.txt` (user-facing)

### HIGH (7 bugs)
5. **Race Conditions in State Management** - `main.py:133,189,292,360`, `llm_router.py:133-165`, `bot/ai_bridge.py:633-646`
   - Load → await → blind write pattern clobbers shared JSON/txt files
   - Fix: Add `filelock` or `threading.Lock`/`asyncio.Lock`; reload state before mutating

6. **Data Loss on File Rotation** - `utils.py:48-49`
   - `%Y-%m-%d` filename overwrites same-day rotations
   - Fix: Append to archive or add timestamp/counter

7. **Unbounded Growth in Nightly Appends** - `nightly_processor.py:88-107`
   - Reads ENTIRE `combined_summaries.txt` + `pdf_exports.txt` into LLM prompt every night
   - Fix: Maintain cursor of last processed byte offset, or limit to last N bytes/lines

8. **Cache Invalidation Flaw** - `ai_processor.py:220`
   - Only hashes first 1000 chars → misses new items at end of long payloads
   - Fix: Hash entire payload

9. **Temporary File / Resource Leaks** - `main.py:202-206, 603-605`, `llm_router.py:531-534`
   - File cleanup bypassed on exceptions; PTY slave FD leaked
   - Fix: Use `try...finally` blocks for `os.unlink` and `os.close(slave)`

10. **Fragile CI/CD Pipeline Logic** - `nightly_processor.py:162-164`
    - `git commit` exits 1 when no changes → script treats as failure, skips push
    - Fix: Check `git status --porcelain` first, or accept exit code 1

11. **Type Mismatch During Timeout Fallback** - `llm_router.py:287, 303, 913, 937`
    - `timeout=min(timeout, 120)` crashes when `timeout` is `httpx.Timeout` object
    - Fix: Enforce `timeout` as `int` at signature, convert to `httpx.Timeout` before request

### MEDIUM (4 bugs)
12. **Malformed JSON Extraction Crash** - `ai_processor.py:347` ✓ FIXED
    - `.split('\n')[0]` breaks multi-line JSON arrays
    - Fix: Remove `.split('\n')[0]`

13. **Unbounded Log Growth** - `ai_processor.py:304` ✓ FIXED
    - `combined_summaries.txt` never rotated
    - Fix: Add rotation using `utils.rotate_file_if_needed`

14. **Hardcoded Chat ID & Config Drift** - `nightly_processor.py:190`, `scrapers/composio_fetcher.py:25-28`
    - Uses `8534649457` instead of `config.SANEL_CHAT_ID`
    - Fix: Import from `config.py`

15. **Dead Code & Redundant Implementations** - `main.py:62-110`, `utils.py:808-850`
    - Unused functions; unsafe dict rate limiter vs existing thread-safe LRU
    - Fix: Remove unused code, switch to thread-safe LRU

### LOW (4 bugs)
16. **Mismatched Emoji in Deduplication Filter** - `ai_processor.py:368` vs `376` ✓ FIXED
    - Missing `📎` in bullet filter tuple
    - Fix: Add `"📎"` to line 376

17. **Unawaited Coroutines in Streaming Callbacks** - `llm_router.py:446`
    - `asyncio.run_coroutine_threadsafe()` results ignored
    - Fix: Attach `done_callback` to log exceptions

18. **Unbounded ClientSession Creation** - `ai_processor.py:49`
    - New `aiohttp.ClientSession` per batch request
    - Fix: Use module-level session

19. **Typo in Replacement Marker** - `ai_processor.py:376`
    - `\"` instead of `\"📎\"`

---

## Source 2: AGY (Gemini 3.1 Pro) - Second Audit (Latest)
[See codebase_audit_report.md at /home/sanel/.gemini/antigravity-cli/brain/d5497a8f-d008-4b77-a1a9-cf4ce61228a8/codebase_audit_report.md]

### CRITICAL
1. **Missing Authentication on Core Commands** - `bot/commands.py`
   - `@require_auth` decorator defined in `bot/security.py` but **completely unused**
   - `summary_command`, `model_command`, `ping_command`, `server_command`, `backup_command` exposed to anyone
   - Fix: Apply `@require_auth` to all command handlers

2. **Blocking I/O in Async Handlers** - `bot/commands.py:summary_command`
   - Calls `get_all_canvas_data()`, `get_classroom_assignments()`, `get_unread_emails()`, etc. SYNCHRONOUSLY on main event loop
   - Entire bot becomes unresponsive during fetch
   - Fix: Move all data gathering into `asyncio.to_thread` or rewrite scrapers with `httpx.AsyncClient`

### HIGH
3. **Notion Rate Limiter Thread Blocking** - `scrapers/notion_client.py`
   - `_RateLimiter.wait()` uses `time.sleep()` while holding `threading.Lock()`
   - Starves thread pool
   - Fix: Use `asyncio.sleep` without holding lock, or release lock before sleeping

4. **Google Token Refresh Race Condition** - `scrapers/google_scraper.py`
   - Time-based caching serves invalid tokens for up to 5 min
   - Fix: Proactive refresh on 401/403 instead of time-based

5. **Missing Timeouts on Network Calls** - `scrapers/mega_study_builder.py`
   - DuckDuckGo/YouTube requests can hang indefinitely
   - Fix: Add explicit timeouts

6. **N+1 API Queries** - Google Docs, Notion deduping
   - Fix: Batch endpoints, pagination

7. **Unbounded State Growth** - `bot/commands.py`
   - `state["seen_tasks"]` grows indefinitely
   - Fix: Cap at 500 entries

### CORRECTNESS
8. **Concurrency Bugs in State Management** - `bot/commands.py`
   - `load_state()`/`save_state()` without locks → data loss
   - Fix: `asyncio.Lock` or migrate to SQLite

9. **Mega Study Builder Fallback Slicing** - `scrapers/mega_study_builder.py`
   - Falls back to `source_context[:8000]` → cuts mid-sentence/JSON
   - Fix: Chunk-based truncation respecting boundaries

### ARCHITECTURE
10. **Incomplete Composio Migration** - `bot/commands.py` vs `scrapers/composio_fetcher.py`
    - Old scrapers still used despite Composio replacement existing
    - Fix: Transition all imports to `composio_fetcher.py`

11. **Tight Coupling & Path Hacks** - Multiple files
    - `sys.path.append(...)` anti-pattern
    - `bot/commands.py` is a God module
    - Fix: Proper package structure, run as `python -m bot.main`

12. **Hardcoded Constants** - `notion_client.py`, `bot/commands.py`
    - `OWNER_ID`, GroupMe ID `102851186`
    - Fix: Move to `.env`/`config.py`

---

## SUMMARY: PRIORITY FIXES

### Already Fixed (this session):
- ✅ Bug 12: JSON parsing multi-line arrays
- ✅ Bug 13: Unbounded log growth (combined_summaries.txt rotation)
- ✅ Bug 16: 📎 emoji typo in deduplication
- ✅ CRITICAL: LLM Router unbounded client creation (using `_get_client()`)
- ✅ HIGH: OpenRouter fallback model variable bug (loop variable)

### Next Priority (CRITICAL from both audits):
1. **Missing @require_auth on commands** - Apply to all handlers in `bot/commands.py`
2. **Blocking I/O in async handlers** - Move scrapers to `asyncio.to_thread` in `summary_command`
3. **Contradictory security filters** - Fix `utils.py` blocklist vs system prompt
4. **Config drift (ai_bridge.py paths)** - Import from `config.py`
5. **Oscillating digest deduplication** - Two-file approach (seen_bullets.json + latest_digest.txt)
6. **Race conditions in state** - Add locks or migrate to SQLite

### Next Priority (HIGH):
7. **Notion rate limiter** - Release lock before sleeping
8. **Google token refresh** - Proactive 401/403 handling
9. **Unbounded growth in nightly appends** - Cursor-based reading
10. **Cache invalidation flaw** - Hash entire payload
11. **Temp file/resource leaks** - try/finally cleanup
12. **Fragile CI/CD** - Check git status before commit
13. **Type mismatch timeout fallback** - Enforce int timeout
14. **Hardcoded chat IDs** - Move to config
15. **Incomplete Composio migration** - Switch imports
16. **Unawaited streaming callbacks** - Add done_callback
17. **Unbounded ClientSession** - Module-level session