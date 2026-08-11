# Sprint Plan: Bug Squashing & Stabilization (21 Bugs)

This plan outlines a 4-sprint strategy (2 weeks per sprint) to fix the remaining 21 bugs identified in the final verification report. The bugs have been consolidated and grouped by theme and priority. Duplicates from the original report (e.g., Security/Security Hardening overlaps and Race Condition overlaps) are unified into single actionable tickets.

## 🏃 Sprint 1: Critical Security & Immediate Stability
**Theme:** Stop data leaks and patch critical security vulnerabilities.
**Goal:** Eliminate all instances of sensitive data exposure, hardcoded secrets, and unsafe bindings.

| Ticket ID | Description | File References | Dependency | Effort |
|-----------|-------------|-----------------|------------|--------|
| **SEC-01**| Scrub API Keys from Logs & Headers | `llm_router.py:46,80` | None | S |
| **SEC-02**| Implement PII Scrubbing in Streaming Path | `llm_router.py:233-239` | None | M |
| **SEC-03**| Sanitize Chat History Files | `main.py:237-244` | SEC-02 | M |
| **SEC-04**| Remove Secrets from Config/Env | `config.py:27-33` | None | M |
| **SEC-05**| Remove Hardcoded Credentials | `activity_log.py:50` | SEC-04 | S |
| **SEC-06**| Fix OAuth Binding to 0.0.0.0 | `google_auth_setup.py:72` | None | S |

* **Definition of Done:** 
  - All keys/secrets are injected via `.env` files or secure secret managers.
  - No API keys appear in application logs or tracebacks.
  - PII scrubbing regex/logic is verified by unit tests.
  - OAuth listens on `127.0.0.1` or explicit authorized hosts only.
* **Risk Assessment:** High risk of breaking authentication flows (SEC-04, SEC-05, SEC-06). Must ensure staging environments have correct secrets before deployment.

---

## 🏃 Sprint 2: Memory, Resources & Input Sanitization
**Theme:** Prevent Out-Of-Memory (OOM) errors, infinite loops, and bad inputs.
**Goal:** Ensure the app handles large tasks and arbitrary user input without degrading performance or crashing.

| Ticket ID | Description | File References | Dependency | Effort |
|-----------|-------------|-----------------|------------|--------|
| **MEM-01**| Cap Unbounded Global Caches (LRU/TTL) | `llm_router.py:32`, `utils.py:194`| None | S |
| **MEM-02**| Close HTTP Clients/File Handles | `scrapers/semantic_retrieval.py:102-135` | None | M |
| **MEM-03**| Chunk Unbounded Lists in Processing | `nightly_processor.py:162-177` | None | M |
| **MEM-04**| Fix Unbounded Recursion | `scrapers/mega_study_builder.py:205-225` | None | M |
| **SEC-07**| Implement Input Validation on User Data | `main.py:524-532` | None | L |

* **Definition of Done:** 
  - Memory profiling confirms stable RAM usage during nightly processing.
  - Context managers (`with` blocks) are used for all file and network I/O.
  - Recursion has explicit base cases and depth limits.
  - User inputs are sanitized (length, type, and content validation).
* **Risk Assessment:** Refactoring list processing (MEM-03) and recursion (MEM-04) may alter the output format of scrapers/processors. Requires regression testing of the resulting data.

---

## 🏃 Sprint 3: Async Operations & Race Conditions
**Theme:** Ensure reliable asynchronous execution.
**Goal:** Prevent silently failing background tasks and eliminate concurrent access issues.

| Ticket ID | Description | File References | Dependency | Effort |
|-----------|-------------|-----------------|------------|--------|
| **ASY-01**| Track `asyncio.create_task()` | `utils.py:897` | None | S |
| **ASY-02**| Fix Missing `await` / Blocking Sync Calls | `ai_processor.py:399` | None | M |
| **ASY-03**| Handle Exceptions in Background Tasks | `main.py:651-656` | ASY-01 | M |
| **CON-01**| Fix Nightly Queue Race Condition | `main.py:692-742` | None | L |

* **Definition of Done:** 
  - All background tasks are stored in a `Set` or use `asyncio.TaskGroup`.
  - All async functions are properly awaited; no blocking I/O (e.g., `time.sleep`, sync `requests`) in the event loop.
  - Nightly queue uses `asyncio.Queue` or proper locking mechanisms (`asyncio.Lock`).
* **Risk Assessment:** Modifying core async loops (ASY-02, CON-01) can cause deadlocks or event loop blocking. Thorough load testing with concurrent simulated users is required.

---

## 🏃 Sprint 4: Reliability & Graceful Degradation
**Theme:** Improve system robustness and standardizing logging.
**Goal:** Allow the application to fail gracefully and recover from transient network/service issues.

| Ticket ID | Description | File References | Dependency | Effort |
|-----------|-------------|-----------------|------------|--------|
| **REL-01**| Implement General Retry Logic | Missing `@retry` decorators | None | M |
| **REL-02**| Fix Error Handling Gaps | `main.py:294-424` | None | M |
| **REL-03**| Graceful Degradation in Voice Handler | `voice_handler.py:36-78` | REL-02 | M |
| **LOG-01**| Standardize & Fix Inconsistent Logging | `telegram_logger.py:19` | None | S |

* **Definition of Done:** 
  - Transient external API errors (e.g., 429, 502) are caught and retried using exponential backoff (e.g., `tenacity`).
  - Voice handler defaults to text or fallback models when primary transcription fails.
  - Logging uses structured formats and correct log levels (`INFO`, `WARNING`, `ERROR`).
* **Risk Assessment:** Adding retries (REL-01) can cause API rate limit exhaustion if not configured properly with backoff and jitter.

---
**Summary of Estimations:**
* **S (Small - < 1 Day):** 7 Tickets
* **M (Medium - 1 to 3 Days):** 10 Tickets
* **L (Large - 3+ Days):** 2 Tickets
* **Total Tickets:** 19 Consolidated Tickets (resolving all 21 specific bug instances mentioned in the report).
