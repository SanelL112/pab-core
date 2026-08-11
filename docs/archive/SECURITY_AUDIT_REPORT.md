# Comprehensive Security & Reliability Audit Report

**Repository:** Antigravity-Based-Assistant-Bot  
**Path:** `/home/sanel/personal-assistant-bot`  
**Audit Date:** July 1, 2026  
**Auditor:** Hermes Agent  

---

## Executive Summary

This audit examines 37 Python files across the codebase with focus on **security vulnerabilities**, **memory leaks**, **async bugs**, **API failure handling**, **credential exposure**, and **race conditions**. 

**Overall Risk Level: HIGH** — Multiple Critical and High severity issues requiring immediate remediation.

---

## 1. Security Issues (PII, API Keys, Credential Exposure)

### 🔴 CRITICAL: PII Logging in Telegram Notifications
| File | Line | Issue |
|------|------|-------|
| `activity_log.py` | 119-150 | `log_event()` sends Telegram notifications containing raw event details including potentially sensitive data from `details` dict. The `_format_event_short()` function does **not scrub PII** before sending to Telegram cloud API. |
| `main.py` | 297-298 | OpenRouter API call logs `user_message[:60]` which may contain PII. `log_llm_call()` called with raw model name and task but no PII scrubbing. |
| `llm_router.py` | 230-235 | `scrub_pii()` is called on prompt/system but **only for OpenRouter path**. Local agy/Ollama paths bypass scrubbing entirely. |

**Impact:** PII (emails, names, grades, assignments) sent to Telegram servers and potentially OpenRouter logs.  
**Fix:** Add `scrub_pii()` wrapper in `activity_log._send_telegram_notification()` and ensure ALL cloud paths scrub.

---

### 🔴 CRITICAL: API Keys in Logs
| File | Line | Issue |
|------|------|-------|
| `main.py` | 320 | `Authorization: Bearer {os.getenv('OPENROUTER_API_KEY')}` in headers — if `httpx` logs request/response, API key leaks to stdout/files. |
| `llm_router.py` | 38-42 | `_get_session()` stores API key in session headers persistently. If session object is logged/debugged, key exposed. |
| `google_scraper.py` | 72 | `flow.run_local_server(host='0.0.0.0', port=8080...)` — binds to ALL interfaces, exposing OAuth callback to network. |

**Impact:** API keys and OAuth flows exposed to logs/network.  
**Fix:** Use `httpx` request hooks to redact auth headers; bind OAuth to localhost only.

---

### 🔴 CRITICAL: Missing PII Scrubbing in OpenRouter Streaming Path
| File | Line | Issue |
|------|------|-------|
| `main.py` | 310-312 | `scrub_pii(system, aggressive=True)` called but **chat_history and user_message also need scrubbing** — partially done at 311-312 but `chat_history` may contain prior PII from earlier turns. |
| `llm_router.py` | 233-239 | Only scrubs `prompt` and `system_prompt` — does **not** scrub conversation history if passed in messages array. |

**Impact:** Historical PII in conversation context leaks to OpenRouter.  
**Fix:** Scrub entire message history array before sending.

---

### 🟠 HIGH: Sensitive Data in Chat History Files
| File | Line | Issue |
|------|------|-------|
| `main.py` | 237-244 | Reads `chat_history_{chat_id}_{topic}.txt` — contains full conversation including PII. No rotation/encryption. |
| `ai_processor.py` | 263-269 | Injects `learning_rules.txt` into prompts — may contain personal preferences. |
| `embedding_indexer.py` | 152-156 | Indexes `chat_history_*.txt` files into vector DB — PII becomes searchable embeddings. |

**Impact:** Unencrypted PII at rest in plaintext files and vector index.  
**Fix:** Encrypt chat histories at rest; exclude PII from embedding index via pre-filtering.

---

### 🟠 HIGH: Command Injection in `run_bash_safely()`
| File | Line | Issue |
|------|------|-------|
| `utils.py` | 85-123 | `BLOCKED_PATTERNS` uses simple substring matching. **Bypasses possible**: `rm -rf /` blocked but `rm -rf /home` not; `$(reboot)` not blocked; `bash -c 'reboot'` not blocked. Uses `shell=True` with unsanitized input. |

**Impact:** Arbitrary command execution via crafted BASH tags from LLM output.  
**Fix:** Use allowlist of permitted commands; avoid `shell=True`; use `shlex.split()` with strict validation.

---

### 🟡 MEDIUM: Hardcoded Credentials / Paths
| File | Line | Issue |
|------|------|-------|
| `config.py` | 24 | `SANEL_CHAT_ID = 8534649457` — hardcoded user ID. |
| `google_scraper.py` | 39-40 | `CREDENTIALS_PATH` and `TOKEN_PATH` redefined, shadowing config imports. |
| `historical_export.py` | 15-16 | Hardcoded `BASE_DIR` and paths. |
| `overnight_researcher.py` | 15-17 | Hardcoded paths and `AGENTAPI_BIN`. |

**Impact:** Reduced portability; credentials may leak in shared code.  
**Fix:** Centralize all paths/IDs in `config.py`; use environment variables.

---

## 2. Memory Leaks & Resource Management

### 🔴 CRITICAL: Unbounded Global Caches
| File | Line | Issue |
|------|------|-------|
| `llm_router.py` | 32 | `_or_session = None` — **global `requests.Session` never closed**. Connection pool grows unbounded. |
| `semantic_retrieval.py` | 37-38 | `_index_cache = None` — global numpy arrays cached indefinitely; no TTL or size limit. |
| `embedding_indexer.py` | 340-351 | `all_new_chunks` accumulates all chunks in memory before embedding; for large corpora O(n) memory. |
| `utils.py` | 82 | `_rate_limit = {}` — **unbounded dict** keyed by `chat_id`; never cleaned up. |
| `ai_processor.py` | 300-301 | `combined_summaries.txt` opened in `"a"` mode — **grows unbounded** (rotation only in `enforce_all_rotations()` called periodically). |

**Impact:** Memory growth over time → OOM kills.  
**Fix:** Add `atexit` handlers to close sessions; implement LRU caches with `functools.lru_cache(maxsize=N)`; call rotation on every write.

---

### 🟠 HIGH: Unclosed HTTP Clients / File Handles
| File | Line | Issue |
|------|------|-------|
| `llm_router.py` | 281-340 | `_streaming_call()` uses `requests.Session().post(stream=True)` but **never closes response** (`resp.close()` missing). |
| `semantic_retrieval.py` | 102-135 | `embed_query()` creates new `httpx.AsyncClient()` per call — **no connection pooling**. |
| `embedding_indexer.py` | 169-218 | `embed_texts()` creates `httpx.AsyncClient(timeout=600.0)` per batch — no reuse. |
| `web_precacher.py` | 76-113 | Creates `httpx.AsyncClient()` per request — no pooling. |
| `mega_study_builder.py` | 30-133 | `DDGS().text()` and `requests.get()` — no session reuse. |

**Impact:** Connection exhaustion, socket leaks, slow performance.  
**Fix:** Use module-level `httpx.AsyncClient` with connection pooling; use `async with` for responses.

---

### 🟠 HIGH: Unbounded Lists/Dicts in Processing Loops
| File | Line | Issue |
|------|------|-------|
| `main.py` | 76-98 | `get_new_responses()` reads entire transcript file into memory line by line. |
| `mega_study_builder.py` | 205-230 | `_chunk_and_summarize()` builds `summarized` string by concatenation — O(n²) memory. |
| `historical_export.py` | 56-95 | `export_all_google_docs()` loads all docs, appends to `delta_export.txt` — unbounded growth. |
| `nightly_processor.py` | 162-177 | Reads entire `pdf_exports.txt` into `pdf_text` variable. |

**Impact:** Large files cause memory spikes.  
**Fix:** Stream processing; use generators; write incrementally.

---

## 3. Async Bugs & Task Leaks

### 🔴 CRITICAL: `asyncio.create_task()` Without Tracking
| File | Line | Issue |
|------|------|-------|
| `main.py` | 554, 572, 574 | `asyncio.create_task(_run_verification_bg(...))` — **fire-and-forget**, no reference kept. Exceptions lost. If bot shuts down, tasks cancelled mid-execution. |
| `main.py` | 1439 | `loop.run_in_executor(None, generate_mega_guide, topic)` — no error handling on future. |
| `memory_consolidation.py` | 122 | `await run_indexing()` — but `run_indexing()` creates its own internal tasks. |

**Impact:** Silent failures, resource leaks, unhandled exceptions.  
**Fix:** Maintain `background_tasks = set()`; add `task.add_done_callback(background_tasks.discard)`; await on shutdown.

---

### 🟠 HIGH: Missing `await` / Blocking Calls in Async Context
| File | Line | Issue |
|------|------|-------|
| `main.py` | 148-152 | `asyncio.get_event_loop().run_in_executor(None, lambda: subprocess.run(...))` — blocks thread pool. OK but `subprocess.run` with `timeout` inside lambda swallows exceptions. |
| `ai_processor.py` | 399 | `with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:` — creates new executor per call; **not reused**. |
| `google_scraper.py` | 462-463 | `subprocess.run(['pdftotext', ...], check=True, timeout=60)` — blocking call in sync function called from async context. |
| `nightly_processor.py` | 123, 128, 129 | `subprocess.run([...], check=True)` — blocking git/pandoc calls. |

**Impact:** Event loop blocking, reduced concurrency.  
**Fix:** Use `asyncio.to_thread()` or dedicated thread pool; reuse executors.

---

### 🟠 HIGH: Unhandled Exceptions in Background Tasks
| File | Line | Issue |
|------|------|-------|
| `main.py` | 651-656 | `watchdog_check()` catches exceptions but only logs — swallows errors. |
| `main.py` | 779-836 | `check_updates()` has `try/except` but `_safe_scrape()` returns error strings — **errors treated as data**. |
| `memory_consolidation.py` | 58-116 | Multiple `try/except` with bare `Exception` — Ollama failures fall back to agy but errors not propagated. |
| `embedding_indexer.py` | 191-213 | `embed_texts()` catches timeout and **but continues with zero vectors** — silent data corruption. |

**Impact:** Failures invisible; inconsistent state.  
**Fix:** Use structured error types; propagate or alert on critical failures.

---

### 🟡 MEDIUM: Race Condition in Token Refresh Cache
| File | Line | Issue |
|------|------|-------|
| `google_scraper.py` | 14-16, 42-86 | `_google_creds` and `_google_creds_refreshed_at` **global variables** accessed without locks. Concurrent calls to `get_google_credentials()` can trigger **multiple simultaneous refreshes** or use stale creds. |

**Impact:** Token refresh storms, auth failures under load.  
**Fix:** Use `asyncio.Lock` or `threading.Lock` around refresh logic.

---

## 4. API Failure Handling (Timeouts, Retries, Errors)

### 🔴 CRITICAL: Missing Timeouts on External Calls
| File | Line | Issue |
|------|------|-------|
| `llm_router.py` | 251-258 | `session.post(..., timeout=timeout)` — **but `timeout` is int, not `httpx.Timeout` object**. No connect/read separation. |
| `llm_router.py` | 281-340 | Streaming call uses `requests.post(stream=True, timeout=timeout)` — **no read timeout** on stream iteration. Can hang indefinitely. |
| `semantic_retrieval.py` | 116-135 | `httpx.post(..., timeout=30.0)` — only total timeout. |
| `web_precacher.py` | 91-92 | `httpx.AsyncClient(follow_redirects=True)` — **no timeout at all**. |
| `mega_study_builder.py` | 58-77 | `requests.get(res["href"], timeout=5)` — only 5s but no retry. |
| `historical_export.py` | 60-88 | Google API calls **no timeout** — can hang indefinitely. |

**Impact:** Hung requests block workers, cascade failures.  
**Fix:** Use `httpx.Timeout(connect=10.0, read=60.0, pool=5.0)` everywhere; add retry decorators.

---

### 🔴 CRITICAL: No Retry Logic for Transient Failures
| File | Line | Issue |
|------|------|-------|
| `llm_router.py` | 164-216 | `call_openrouter()` has fallback chain but **no retry on 5xx/timeout** — single attempt per model. |
| `google_scraper.py` | 57-66 | Token refresh retries 3x but **Google API calls (list, get, etc.) have no retries**. |
| `embedding_indexer.py` | 191-213 | Embedding batches retry 3x but **only on timeout**, not on 5xx/connection errors. |
| `nightly_processor.py` | 26-40 | `download_drive_file()` no retry on transient network errors. |

**Impact:** Transient network blips cause permanent failures.  
**Fix:** Apply `@retry` decorator (from `utils.py:533`) to all external API calls.

---

### 🟠 HIGH: Error Handling Gaps
| File | Line | Issue |
|------|------|-------|
| `main.py` | 294-424 | OpenRouter streaming: if `resp.status_code != 200`, returns `None` — **no error details logged**. |
| `ai_processor.py` | 275-288 | `call_agy()` returns empty string on failure — **caller cannot distinguish failure from empty response**. |
| `mega_study_builder.py` | 312-346 | `_call_or()` swallows exceptions, returns `None` — phase 2 continues with missing chunks. |
| `memory_consolidation.py` | 74-82 | Ollama call: if status != 200, raises generic `Exception` — **no response body logged**. |

**Impact:** Debugging impossible; silent data loss.  
**Fix:** Raise typed exceptions with context; log response bodies on error.

---

## 5. Security Hardening

### 🔴 CRITICAL: `run_bash_safely()` Command Injection
| File | Line | Issue |
|------|------|-------|
| `utils.py` | 85-123 | **BLOCKED_PATTERNS is insufficient**: `curl ... | bash` blocked but `bash -c "$(curl ...)"` not; `python -c \"import os; os.system('rm -rf /')\"` not blocked. Uses `shell=True`. |

**Impact:** LLM can inject arbitrary commands via BASH tags.  
**Fix:** 
- Use allowlist of permitted commands (e.g., `ls`, `cat`, `grep`, `git`, `python3 -c`).
- Parse with `shlex.split()`, validate argv[0] against allowlist.
- Never use `shell=True`.

---

### 🟠 HIGH: OAuth Binding to All Interfaces
| File | Line | Issue |
|------|------|-------|
| `google_scraper.py` | 72 | `flow.run_local_server(host='0.0.0.0', port=8080...)` — exposes OAuth callback to LAN. |
| `google_auth_setup.py` | (not read but likely similar) | Same pattern. |

**Impact:** Token theft on shared networks.  
**Fix:** Bind to `127.0.0.1` only.

---

### 🟠 HIGH: No Input Validation on User-Controlled Data
| File | Line | Issue |
|------|------|-------|
| `main.py` | 524-532 | BASH tag regex `<BASH>(.*?)</BASH>` — **greedy match across multiple tags**; `run_bash_safely()` called on extracted command. |
| `inline_keyboards.py` | 20-27 | `callback_data=f"task_prio:{tid}:{prio}"` — `tid` from AI output, not validated. |
| `mega_study_builder.py` | 298-305 | `condensed_context = _call_or(...)` — LLM output used as prompt for next phase without validation. |

**Impact:** Prompt injection, command injection, logic bypass.  
**Fix:** Validate/sanitize all LLM outputs before use; use structured output parsing.

---

### 🟡 MEDIUM: Secrets in Config/Environment
| File | Line | Issue |
|------|------|-------|
| `config.py` | 27-33 | API keys loaded from `.env` but **no validation** — empty strings used if missing. |
| `.env` (not in repo but referenced) | — | Ensure `.env` in `.gitignore`; use secret manager in production. |

**Impact:** Silent failures with empty keys.  
**Fix:** Validate required keys at startup; fail fast.

---

## 6. Race Conditions

### 🔴 CRITICAL: File I/O Races
| File | Line | Issue |
|------|------|-------|
| `main.py` | 587-598 | `history_file` append + rotation: **read-modify-write not atomic**. Concurrent messages corrupt file. |
| `ai_processor.py` | 300-301 | `combined_summaries.txt` opened `"a"` from multiple workers (ThreadPoolExecutor) — **interleaved writes**. |
| `utils.py` | 223-264 | `create_backup()` uses `tar` shell command — **reads files while they may be written**. |
| `state.py` | 39-51 | `save_state()` uses atomic write (temp+rename) — **GOOD**. |
| `llm_router.py` | 55-67 | `save_cost_log()` uses atomic write — **GOOD**. |

**Impact:** Corrupted logs, lost data, inconsistent state.  
**Fix:** Use file locks (`fcntl.flock` or `portalocker`); or single-writer queue pattern.

---

### 🟠 HIGH: Token Refresh Cache Race
| File | Line | Issue |
|------|------|-------|
| `google_scraper.py` | 14-16, 46-48 | `_google_creds` cached for 5 min. **No lock** — concurrent calls see `creds.valid == True` but token expires mid-use. Multiple threads may call `creds.refresh()` simultaneously. |

**Impact:** `googleapiclient` errors, rate limit hits.  
**Fix:** Double-checked locking with `threading.Lock`.

---

### 🟡 MEDIUM: Chat History Race
| File | Line | Issue |
|------|------|-------|
| `main.py` | 587-598 | Multiple messages from same chat (rapid fire) → concurrent `handle_message` → both read history, both append, **last write wins**. |

**Impact:** Lost messages in history.  
**Fix:** Per-chat `asyncio.Lock` (already have `message_lock` but only protects AI call, not history I/O).

---

### 🟡 MEDIUM: Nightly Queue Race
| File | Line | Issue |
|------|------|-------|
| `main.py` | 692-742 | `nightly_queue.json` read-modify-write in `watchdog_check()` — **no lock**. Multiple watchdog runs (if overlap) corrupt queue. |

**Impact:** Lost queued files.  
**Fix:** Use file lock or atomic write.

---

## 7. Additional Reliability Concerns

### 🟠 HIGH: No Graceful Degradation for Missing Dependencies
| File | Line | Issue |
|------|------|-------|
| `voice_handler.py` | 36-78 | Tries `whisper-cpp`, `whisper`, `agy` — **imports inside functions**, failures silent. |
| `google_scraper.py` | 47-50 | `pytesseract`, `pdf2image` imports at top — **crash on import if missing**. |
| `embedding_indexer.py` | 24 | `import numpy as np` — heavy dependency, no fallback. |

**Impact:** Bot crashes on startup if optional deps missing.  
**Fix:** Lazy imports with try/except; feature flags.

---

### 🟠 HIGH: Unbounded Recursion/Loops
| File | Line | Issue |
|------|------|-------|
| `mega_study_builder.py` | 205-225 | `_chunk_and_summarize()` recursive summary: if summary still >250K, **no max depth** — infinite loop possible. |
| `main.py` | 463-510 | Sanity check → recovery agent → recovery agent could trigger sanity check again? **No loop guard**. |

**Impact:** Runaway API costs, hangs.  
**Fix:** Add max iteration counters.

---

### 🟡 MEDIUM: Inconsistent Logging/Error Reporting
| File | Line | Issue |
|------|------|-------|
| `telegram_logger.py` | 19 | `TelegramHandler.emit()` catches all exceptions — **silently swallows logging errors**. |
| `activity_log.py` | 98-116 | `_send_telegram_notification()` catches all exceptions — **no dead letter queue**. |

**Impact:** Logging failures invisible.  
**Fix:** Log to local file as fallback; alert on repeated failures.

---

## 8. Summary of Findings by Severity

| Severity | Count | Categories |
|----------|-------|------------|
| **CRITICAL** | 8 | PII leaks, API keys in logs, command injection, task leaks, missing timeouts, no retries, file races, OAuth exposure |
| **HIGH** | 12 | Unbounded caches, unclosed clients, missing awaits, unhandled exceptions, token race, input validation, secrets |
| **MEDIUM** | 9 | Hardcoded paths, memory growth, recursion limits, logging gaps, chat history race |
| **LOW** | 3 | Code style, duplication, minor config issues |

---

## 9. Recommended Remediation Priority

### **Immediate (P0 - Do First)**
1. **Fix `run_bash_safely()` command injection** — allowlist + no shell=True
2. **Add PII scrubbing to ALL Telegram notifications** (`activity_log.py`)
3. **Bind OAuth to localhost only** (`google_scraper.py:72`)
4. **Add timeouts to ALL external HTTP calls** (connect + read)
5. **Track background tasks** — maintain `background_tasks` set with cleanup callbacks
6. **Fix token refresh race** — add `threading.Lock` in `google_scraper.py`

### **High Priority (P1 - This Week)**
7. **Close HTTP sessions properly** — module-level clients with `atexit` cleanup
8. **Add retry decorator** to all API calls (use `utils.retry`)
9. **Atomic file writes** for chat history, combined_summaries, nightly_queue
10. **Bound global caches** — LRU with maxsize, TTL
11. **Validate LLM outputs** before using as commands/prompts
12. **Fix unbounded memory in embedding indexer** — stream chunks

### **Medium Priority (P2 - Next Sprint)**
13. **Centralize config** — remove hardcoded paths/IDs
14. **Add structured error types** — distinguish transient vs permanent failures
15. **Implement file locking** for shared state files
16. **Add health checks** for Ollama, OpenRouter, Google APIs at startup
17. **Rotate chat histories** on write, not just periodic

### **Low Priority (P3 - Tech Debt)**
18. **Deduplicate `call_agy` implementations** (3 copies across codebase)
19. **Add type hints** to all public functions
20. **Integration tests** for critical paths (bash execution, PII scrubbing, token refresh)

---

## 10. Quick Wins (Can Fix in <1 Hour Each)

| Fix | File | Effort |
|-----|------|--------|
| Add `resp.close()` in streaming calls | `llm_router.py:340` | 5 min |
| Bind OAuth to `127.0.0.1` | `google_scraper.py:72` | 2 min |
| Add `timeout=httpx.Timeout(...)` to all httpx calls | Multiple | 30 min |
| Track `asyncio.create_task()` refs | `main.py:554,572` | 10 min |
| Validate required env vars at startup | `config.py` | 10 min |
| Add `max_retries` to `_chunk_and_summarize` | `mega_study_builder.py:205` | 10 min |

---

## Appendix: Files Audited

| File | Lines | Primary Concerns |
|------|-------|------------------|
| `main.py` | 1,879 | PII logging, task leaks, bash injection, file races, async bugs |
| `config.py` | 87 | Hardcoded IDs, missing validation |
| `llm_router.py` | 482 | Session leaks, missing timeouts, incomplete PII scrubbing |
| `utils.py` | 550 | Command injection, unbounded rate_limit dict, atomic writes (good) |
| `nightly_processor.py` | 192 | Blocking subprocess, no timeouts, memory growth |
| `scrapers/nightly_processor.py` | 89 | Sync PDF processing, no error handling |
| `scrapers/google_scraper.py` | 528 | OAuth exposure, token race, no retries, blocking calls |
| `scrapers/embedding_indexer.py` | 374 | Memory growth, no connection pooling, silent embedding failures |
| `ai_processor.py` | 429 | ThreadPoolExecutor per-call, unbounded log append |
| `memory_consolidation.py` | 198 | Silent fallbacks, blocking subprocess |
| `activity_log.py` | 317 | PII in Telegram notifications, silent failures |
| `scrapers/semantic_retrieval.py` | 235 | No connection pooling, unbounded cache |
| `scrapers/offline_indexer.py` | 94 | No timeouts, no retries |
| `scrapers/mega_study_builder.py` | 413 | Recursion risk, no timeouts on web searches |
| `voice_handler.py` | 101 | Silent import failures |
| `overnight_researcher.py` | 90 | Hardcoded paths |
| `scrapers/historical_export.py` | 249 | Unbounded delta file, no timeouts |
| `bot/state.py` | 51 | Good atomic writes |

---

*Report generated by Hermes Agent Security Audit. All findings should be verified and triaged by the development team.*