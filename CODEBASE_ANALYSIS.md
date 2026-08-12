# Codebase Overview & Architecture

> Deep architectural analysis of the `personal-assistant-bot` codebase.
> Derived from first-hand reads of the source (not just the docs), with
> file:line references for load-bearing logic.

---

## 1. Executive Summary

**What it is.** A single-owner, privacy-first **personal assistant bot** fronted
by **Telegram** (`python-telegram-bot`), running continuously on a
resource-constrained home server (Intel i5-3210M, ~5.7 GB RAM). It acts as a
hybrid **academic assistant, knowledge indexer, and home-lab operator**: it
scrapes LMS/email sources (Canvas, Google Classroom, Gmail, Notion, GroupMe),
builds and queries a semantic vector "brain," generates study guides, manages a
private assignment calendar, and exposes server-admin commands over chat.

**Defining design stance.** The codebase's single clearest theme is
**privacy-by-design with fail-closed cloud egress**. Every inference request is
data-classified (`PUBLIC` / `PERSONAL` / `PII`) and routed such that private data
*never* leaves owner-controlled hardware unless a caller passes an explicit,
per-call cloud-consent flag. Scrubbing is explicitly treated as **defense in
depth, not consent** (`llm_router.py:1-14`).

**Key technologies & runtime.**

| Layer | Choice | Notes |
|-------|--------|-------|
| Language / runtime | Python 3.10+ | asyncio via `python-telegram-bot[job-queue]==22.8` |
| Bot framework | `python-telegram-bot` | `ApplicationBuilder`, `JobQueue` (daily jobs), inline keyboards |
| Local LLM inference | **Ollama** (`:11434`) + **llama.cpp RPC cluster** | 3-node cluster (Surface orchestrator + Dell + Orange Pi 5 workers) |
| Cloud LLM inference | **OpenRouter** (primary) → **Opencode Zen** → **Hack Club AI** | Free-tier models, sequential rate-limit fallback |
| Embeddings / retrieval | Ollama `nomic-embed-text` (dim 768) + **NumPy** cosine top-K | `.npz` vector index, MD5-incremental rebuild |
| OCR / multimodal | `pytesseract`, `Pillow`, `pdf2image`, `PyPDF2` | Photo + Classroom-PDF OCR pipelines |
| External APIs | `google-api-python-client`, `canvasapi`, `selenium`, Notion, Composio | Google OAuth token; Canvas via persistent Firefox/ClassLink session |
| Storage | **Plain files** (Markdown + JSON + SQLite) | No DB server; atomic JSON store + rotation |
| Deployment | **systemd** (`bot.service`), hardened unit | Tailscale-gated network readiness |

**Architectural shape.** Two coexisting layers: a newer, deliberately hardened
**`bot/` package** (security, transactional state, atomic storage, UI, AI bridge)
wrapping — and gradually replacing — an older set of **flat top-level modules**
(`utils.py`, `ai_processor.py`, `llm_router.py`, `config.py`) plus a large
**`scrapers/` engine**. This is a living **refactor-in-place**.

---

## 2. Directory & Component Structure

High-level map (annotated with responsibility):

```
personal-assistant-bot/
├── main.py                  # Entry point: PTB app, handler registration, JobQueue
│                            #   schedules (nightly 1AM / morning 7AM ET), background
│                            #   digest job (Notion sync, fuzzy task dedup), locks.
├── config.py                # Env-driven settings (side-effect-free import), PAB_*
│                            #   root overrides, all state-file paths, model policy,
│                            #   RPC cluster sizing, initialize_runtime()/validate_*.
├── llm_router.py            # Unified LLM dispatch: trust/sensitivity model,
│                            #   OpenRouter/Ollama/agy/RPC callers, fallback chains,
│                            #   cost tracking, response validation, RPC cluster mgmt.
├── ai_processor.py          # Per-source processing pipeline: Orange Pi classifier
│                            #   gate, LLM task/topic extraction, digest assembly.
├── bot/                     # NEW hardened package (the refactor target layer)
│   ├── security.py          #   require_auth decorator (handler boundary authZ)
│   ├── state.py             #   Transactional state: update_state() over
│   │                        #   AtomicJSONStore; per-user asyncio locks; sleep window
│   ├── storage.py           #   AtomicJSONStore: durable, private JSON primitives
│   ├── ai_bridge.py         #   send_to_antigravity_and_wait(): privacy-preserving
│   │                        #   bridge from chat -> retrieval -> local/cloud inference
│   ├── commands.py          #   All /command handlers (~1171 lines)
│   ├── ui.py                #   Shared Telegram presentation (progress, rendering)
│   ├── dashboard_state.py   #   Privacy-safe routing state for the status kiosk
│   └── runtime.py           #   Background task tracking for clean shutdown
├── scrapers/                # "The Engine": all data ingestion + knowledge build
│   ├── memory_consolidation.py  # Nightly pipeline orchestrator (phases 1-5)
│   ├── embedding_indexer.py     # Build vector index (nomic-embed-text -> .npz)
│   ├── semantic_retrieval.py    # Query-time cosine top-K retrieval (+ fallback)
│   ├── google_scraper.py        # Gmail / Classroom / Calendar / Drive / Docs
│   ├── canvas_scraper.py        # Canvas LMS via local Firefox/ClassLink session
│   ├── notion_client.py         # Notion task sync
│   ├── groupme_scraper.py       # GroupMe source
│   ├── nightly_processor.py     # OCR pipeline + append-only study-guide updates
│   ├── mega_study_builder.py    # Multi-stage "Editor-in-Chief" textbook builder
│   ├── offline_indexer.py       # Incremental local historical index
│   ├── assignment_calendar.py   # Private CalDAV/Google assignment calendar
│   └── ... (compile_context, web_precacher, historical_export, rescue_docs, ...)
├── surface/                 # RPC cluster management (cluster_manager.py, start script)
├── utils.py                 # LEGACY grab-bag: PII scrubber, safe-bash, rotation,
│                            #   backups, correlation graph, health status
├── activity_log.py          # JSONL event log (5MB rotation), Telegram notify
├── voice_handler.py         # Local voice transcription (Whisper path)
├── inline_keyboards.py      # Telegram inline-keyboard builders
├── tests/                   # 20 pytest files (security, routing, state, reliability)
├── bot.service              # Hardened systemd unit
└── [state & data files]     # state.json, curated_brain.md, mega_index.md,
                             #   nightly_queue.json, embedding_data/*.npz, ...
```

**Core modules and their explicit responsibilities.**

- **`main.py`** — *Composition root.* Owns the PTB `Application`, registers every
  command/message/voice/photo handler (`main.py:984-1004`), and installs the two
  `run_daily` jobs (nightly at 01:00 ET, morning digest at 07:00 ET;
  `main.py:978-981`). Also hosts the periodic `check_updates` background job that
  turns scraped data into Notion tasks and digests.
- **`config.py`** — *Settings & filesystem layout.* Import is intentionally
  side-effect free (`config.py:1-7`); the entry point explicitly calls
  `initialize_runtime()` (creates private dirs `0o700`, hardens secret files to
  `0o600`; `config.py:213-227`) and `validate_runtime_config(feature)` which
  checks only what a given feature needs (`config.py:230-249`).
- **`llm_router.py`** — *Inference boundary.* Centralizes every LLM call and
  enforces the cloud-egress policy (see §4).
- **`ai_processor.py`** — *Source pipeline.* Classifies raw scraped items through
  the Orange Pi classifier (`PI_CLASSIFIER_URL`, `ai_processor.py:37-68`), then
  uses an LLM to extract tasks/topics and assemble the daily digest.
- **`bot/` vs legacy flat modules** — The `bot/` package is where *new*, hardened
  logic lives (transactional state, authZ, typed AI bridge). Older helpers remain
  at top level and are being migrated inward.

---

## 3. Data Flow & Core Workflows

### 3.1 Interactive chat (the hot path)

```
Telegram message
   └─ @require_auth  (bot/security.py:34)  ← private-chat + owner-user-id gate
      └─ is_sleep_window()? (bot/state.py:97)  → refuse during 1–7 AM ET
         └─ handle_message (main.py:605)
            └─ per-user asyncio lock (bot/state.py:84)  ← serialize per chat
               └─ send_to_antigravity_and_wait (bot/ai_bridge.py)
                  ├─ semantic_retrieval.get_context_for_prompt(query)  (RAG)
                  │    └─ cosine top-K over embedding_data/embedding_index.npz
                  │       (falls back to get_fallback_context() tail-read)
                  └─ route by data classification (llm_router):
                       PRIVATE/PERSONAL → call_local_rpc_result
                          (Surface RPC → Pi Ollama → Dell Ollama; cloud BLOCKED)
                       PUBLIC + explicit consent → call_openrouter_result
                          (OpenRouter → Opencode Zen → Hack Club)
                  └─ PII scrub (utils.scrub_pii) as defense-in-depth before any
                     cloud payload
```

**Key interaction facts:**

- **Authorization** is a decorator asserted on *every* handler. It requires the
  chat to be `ChatType.PRIVATE`, `chat_id == TELEGRAM_CHAT_ID`, and
  `user_id == TELEGRAM_OWNER_USER_ID` (`bot/security.py:24-30`). Unauthorized
  callers get a polite rejection and a warning log.
- **Serialized inference**: a per-user `asyncio.Lock` (held in a
  `WeakValueDictionary` so historical chats don't leak locks) ensures one request
  per user at a time (`bot/state.py:18-90`). Module-level `digest_lock` and
  `watchdog_lock` (`main.py:598-599`) prevent overlapping background cycles.
- **Streaming UX**: cloud streaming renders "Thinking…/Drafting…" by editing a
  status message via `asyncio.run_coroutine_threadsafe` into the PTB main loop
  (`llm_router.py:544-638`).

### 3.2 Background digest → Notion task sync (`check_updates`)

`check_updates` (the periodic job) runs the per-source pipeline and then:

1. **Fuzzy dedup** of candidate tasks against `state["seen_tasks"]` using
   `difflib.SequenceMatcher > 0.8` (`main.py:390-396`).
2. **Push to Notion** for each genuinely-new task (`main.py:408-421`).
3. **Record interactive task controls** in state (`pending_tasks`, capped at 100)
   so Telegram inline buttons can update the matching Notion row *without a second
   LLM pass* (`main.py:456-493`).
4. **Send digest** (unless it is the "✅ All caught up" quiet digest) and offer
   one-tap study-guide generation for detected topics (`main.py:528-557`).
5. **Track cross-source correlations** and bound `seen_tasks` to
   `MAX_SEEN_TASKS` (`main.py:560-572`).

### 3.3 Nightly knowledge pipeline (`scrapers/memory_consolidation.py`)

Runs ~1–2 AM via the `run_daily` nightly job. Ordered, fault-tolerant phases:

```
Phase 0  Bootstrap embedding index if missing (rebuild_index_if_missing)
Phase 1  LLM compresses the day's logs -> curated_brain.md ("Memory Consolidation Engine" prompt)
Phase 2  Incremental local historical index (offline_indexer.run_indexing)
Phase 3/5  Embedding index rebuild (embedding_indexer.build_index via nomic-embed-text)
           └─ on success: semantic_retrieval.invalidate_cache()
(+ daily Classroom-PDF download + OCR to .txt in the 2 AM nightly_processor)
```

Each phase is wrapped so a failure is logged (and categorized via `activity_log`)
without aborting the rest — appropriate for an unattended batch job on a box that
must be healthy by morning.

### 3.4 Embedding / semantic retrieval (RAG "memory")

**Build** (`scrapers/embedding_indexer.py`):

- Walks all text sources (`collect_sources()`), hashing each with **MD5** so only
  changed sources are re-embedded (incremental manifest; `embedding_indexer.py:100-149`).
- Chunks at **~1000 tokens with ~200-token overlap** (`CHUNK_SIZE`/`CHUNK_OVERLAP`,
  `embedding_indexer.py:45-46`).
- Embeds with Ollama `nomic-embed-text` (**dim 768**; `embedding_indexer.py:44-48`),
  retrying; on exhausted retries it inserts **zero vectors** so the index still
  serializes (retrieval naturally downweights zero rows via cosine similarity).
- Writes **atomically** via `np.savez_compressed` staging and validates the loaded
  index (e.g. rejects zero rows; `_validate_loaded_index`, `embedding_indexer.py:327-356`).

**Query** (`scrapers/semantic_retrieval.py`):

- Normalizes stored vectors once, then `scores = vectors @ query_vec` (dot product
  == cosine for unit vectors; `semantic_retrieval.py:293-302`) — sub-millisecond for
  ~5K chunks.
- Caches the loaded index (`invalidate_cache()` called after each nightly rebuild).
- **`get_fallback_context()`** (tail-read of `curated_brain.md` + latest digest) is
  used when Ollama or the index is unavailable — a graceful-degradation pattern.

### 3.5 Local inference fabric (3-tier, OOM-guarded)

`call_local_rpc` (`llm_router.py:833`) is the primary *private* path, with the
fallback chain:

```
1. Surface llama-server (10.0.0.47:8080)   ← RPC orchestrator, LOADS the model
2. Orange Pi 5 Ollama  (10.10.10.2)        ← fast local backup
3. Dell local Ollama
4. Cloud (ONLY if allow_cloud AND explicit public consent)
```

- A **single monotonic deadline** is shared across the whole chain; Surf-face's
  attempt is capped by `RPC_SURFACE_TIMEOUT`, which is hard-clamped to stay at
  least 60 s under `RPC_INFERENCE_TIMEOUT` so a Surface stall can never starve the
  Pi/Dell fallbacks (`config.py:170-185`, `llm_router.py:867-937`).
- **Memory guard** (`check_rpc_memory_ok`, `llm_router.py:1201`): only the
  *Surface orchestrator's* free RAM can veto (it is the node that OOMs at
  model-load). Worker RAM is reported for observability **only** — because workers
  legitimately hold offloaded layers and "low free RAM" is the *healthy* state.
- Legacy overnight/batch path `call_llamacpp_rpc_with_fallback` (`llm_router.py:1255`)
  validates memory + server health before each attempt, then falls back to local
  Ollama, then (optionally) cloud.

### 3.6 Private assignment calendar (opt-in, conservative by design)

Disabled by default; writes nothing until the owner runs `/calendar enable`.
Sources: Canvas (timed deadlines → 24 h + 1 h reminders), Google Classroom and
Notion (date-only → exact 7 AM local CalDAV reminder). Delivery via a
localhost-bound **Radicale CalDAV** service (`127.0.0.1:5232`, exposed only over
Tailscale), optionally mirrored to a *dedicated* Google Calendar via verified
Composio action slugs. Reconciliation only applies create/update/complete deltas
and **never infers deletion from a missing source item** — an expired session or
API blip must not wipe calendar events.

### 3.7 Backups & retention

`create_backup()`/`restore_backup()`/`cleanup_old_backups()` (`utils.py:519-597`)
snapshot the files in `config.BACKUP_FILES` (state, curated brain, mega index,
digest, correlation graph, cost log, nightly queue/dead-letter, runtime SQLite)
into `backups/` with a 30-day retention (`config.py:196-201`). A daily backup job
is scheduled in `main.py:954-961`.
