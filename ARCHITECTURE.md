# pab-core — Architecture & Deep Reasoning

> Audience: an AI agent or engineer who must reason about, modify, or operate this
> system. This document explains **why** the system is shaped the way it is, the
> invariants that must not be violated, and the failure modes that have already bitten.
> The human-facing overview is in `README.md`.

---

## 1. System purpose & context

pab-core is the application half of a self-hosted, privacy-preserving personal
assistant for a single owner (a high-school student). It is not a general product;
it is a **single-tenant, owner-only** system. Every design decision favors (a) keeping
personal/academic data on local hardware, (b) never breaking the live bot, and
(c) surviving unattended overnight batch runs without corrupting state.

It is one of three repos split from a former monorepo:

- **pab-core** (this repo, public) — application code + service entry points.
- **pab-ops** (public) — systemd units, health checks, RPC/cluster/inference infra.
- **pab-study-content** (private) — generated study guides + knowledge base (data).

The split boundary is **coupling-driven, not aesthetic**: see §6.

---

## 2. Runtime topology (what is actually running)

Five systemd services on the host `sanel` (Debian 13, kernel 6.12). Four are defined
by *this* repo's code; the fifth is infra.

```
bot.service ─────────────► main.py                    (Telegram long-poll loop)
canvas-browser.service ──► scripts/canvas_browser_daemon.py  → 127.0.0.1:8976
pab-dashboard-agent ─────► scripts/dashboard_agent.py        → 0.0.0.0:8765
assignment-caldav ───────► radicale (venv) + external conf   → 0.0.0.0:5232
llama-rpc.service ───────► (defined in pab-ops)              (local inference)
```

Key operational facts:
- `bot.service` `WorkingDirectory` and `PAB_CONFIG_DIR`/`PAB_RUNTIME_DIR` (set via a
  drop-in `/etc/systemd/system/bot.service.d/release.conf`) all point at the repo root.
- The Canvas daemon holds a **persistent authenticated Firefox/ClassLink session** and
  exposes it over a localhost-only HTTP endpoint (`:8976/health`). Canvas is scraped
  through this browser session, **never** through an API token — this is deliberate,
  because ClassLink SSO cannot be reduced to a static token.
- `sudo` on this host is **NOT passwordless**. Any service/unit change must be handed
  to the human as a single-line command (heredocs hang the operator's terminal).
  Commands prefixed with `!` in the operator's shell can silently no-op — always verify
  live state (`systemctl show`, PID uptime, `curl` health) after a change.

---

## 3. Layered architecture & data flow

Four logical layers, all inside one Python package tree (no cross-repo imports at
runtime):

```
 SOURCES                INGEST (scrapers/)            CORE                 SURFACE (bot/)
 ┌─────────┐            ┌──────────────────┐    ┌───────────────┐    ┌──────────────┐
 │ Canvas  │─browser──► │ canvas_scraper   │    │ config.py     │    │ main.py      │
 │ Google  │─composio─► │ google_scraper   │──► │ utils         │◄──►│ commands.py  │
 │ GroupMe │──────────► │ groupme_scraper  │    │ activity_log  │    │ ai_bridge    │
 │ Notion  │──────────► │ notion_client    │    │ llm_router ───┼──► │ ui.py        │
 └─────────┘            └────────┬─────────┘    │ storage/state │    │ security     │
                                 │              └───────┬───────┘    └──────┬───────┘
                     ┌───────────▼───────────┐          │                   │
                     │ morning_digest        │          ▼                   ▼
                     │ mega_study_builder    │   local inference       Telegram user
                     │ embedding_indexer     │   (Ollama / RPC)        (owner only)
                     │ semantic_retrieval    │
                     │ nightly_processor     │
                     └───────────┬───────────┘
                                 ▼
                     assignment_calendar ──► CalDAV (:5232) + Google Calendar
```

**Primary flows:**

1. **Digest loop** (every `DIGEST_INTERVAL_SECONDS`, default 4h): scrapers pull new
   source items → deduped against `state.json` (`seen_tasks`, bounded) → summarized by
   the LLM router → pushed to Telegram as a digest with inline-keyboard actions.
2. **Chat**: user message → `bot/security` (authorize owner) → `bot/ai_bridge`
   (classify sensitivity, scrub PII) → `llm_router` (route local-vs-cloud) → rendered
   via `bot/ui` → reply.
3. **Assignment calendar**: Canvas/Docs deadlines → `assignment_calendar` → written to
   the private Radicale CalDAV server + optionally proposed to Google Calendar
   (approval-gated for Docs-derived deadlines).
4. **Overnight batch**: `nightly_processor` drains a queue of study docs losslessly,
   with a dead-letter file for failures, and rebuilds the embedding index.

---

## 4. The LLM router — the most important subsystem

`llm_router.py` is the trust and cost boundary for all inference. Reason about it
carefully before changing anything.

- **Trust classification.** `ProviderTrust` ∈ {LOCAL, CLOUD}. Providers matching
  markers `("ollama","llama.cpp","llamacpp","rpc","local")` are LOCAL; everything else
  is CLOUD. `Sensitivity` tags the *request*.
  **Invariant: sensitive/private data must only go to LOCAL providers.** Cloud is for
  work explicitly classified non-sensitive. Do not weaken this.
- **Fallback chain.** Local RPC cluster → local Ollama → cloud (OpenRouter free tiers:
  `OR_DEFAULT_MODEL` → `OR_FALLBACK_MODEL` → `OR_THIRD_MODEL`; HackClub AI as another
  option). A dead provider must degrade, not crash the assistant.
- **Ollama dual-box gotcha (already bit us):** requests for tiny models (e.g.
  `qwen2:0.5b`, `qwen2.5:3b`) auto-route to `OLLAMA_ORANGEPI_URL` (the Orange Pi 5 at
  `10.10.10.2:11434`). If that returns empty, the router **must** retry against the
  local box `OLLAMA_URL` — otherwise the bot silently returns `""` to callers. This
  retry exists; preserve it.
- **RPC cluster timeouts share one deadline.** A per-node cap equal to the total budget
  kills fallbacks. `RPC_SURFACE_TIMEOUT` and the fallback reserve are tuned so the
  fallback actually gets a turn. Measure before retuning (see `pab-ops` runbook).
- **Cost tracking.** Every call is logged to `COST_LOG_FILE`. Cloud is treated as a
  scarce/paid resource even on "free" tiers.

---

## 5. State, durability & privacy invariants

- **Canonical state file is `config.STATE_FILE` → `state.json`.** `bot_state.json` is a
  stale decoy — ignore it. `state.json.bak` is a backup, gitignored.
- **All state writes are atomic/transactional** (`bot/storage.py`'s `AtomicJSONStore`,
  used by `bot/state.py` and several scrapers). Never introduce a naive
  `open(...,'w')` write to shared state; a crash mid-write must not corrupt it.
- **Bounded growth.** `seen_tasks`/`seen_alerts` are capped (`MAX_SEEN_TASKS`) so the
  dedup set can't grow unbounded. Rotations enforced via `utils.enforce_all_rotations`.
- **PII scrubbing is mandatory on any egress that leaves the trust boundary** — logs,
  telemetry, cloud calls. Use `utils.scrub_pii`. `activity_log.py` is privacy-preserving
  by construction.
- **Owner-only authorization.** `bot/security.require_auth` gates every external Telegram
  handler. The only authorized identity is `TELEGRAM_OWNER_USER_ID`.
- **Secrets** live only in `.env` / `credentials.json` / `token.json`, all gitignored.
  History was audited clean (the one `AIzaSy…` string in an old log is YouTube's public
  web-client key, not a credential).

---

## 6. Why this is ONE package, not six (the import-cycle reality)

A naive "one repo per layer" split is **impossible without a refactor**, because the
core and surface layers import each other:

- `utils.py` → `from bot.state import update_state`
- `llm_router.py` → `from bot.dashboard_state import record_route`, `from bot.ui import render_assistant_text`
- `scrapers/composio_fetcher.py`, `scrapers/nightly_processor.py` → `from bot.storage import AtomicJSONStore`

`config.py` is imported by ~35 modules and is the universal keystone. Because
"shared core" (utils/llm_router) and the `bot/` package have a **mutual import cycle**,
they cannot live in separately-installable repos without first hoisting
`bot.storage`/`bot.state.update_state`/`bot.ui.render_assistant_text`/
`bot.dashboard_state.record_route` into a dependency-free core module. Until that
refactor happens, **core + ingest + surface ship together** (this repo). Only
truly-decoupled things were split out: infra (pab-ops) and data (pab-study-content).

If you intend a finer split later: break the cycle first (move the four
`bot.*` symbols above into a `pab/core/` module), prove `pytest` still passes, then
extract.

---

## 7. Known failure modes / traps (learned the hard way)

- **Fresh clone was not runnable** until 2026-08: 8 files the live services import
  (`bot/ui.py`, `bot/storage.py`, `bot/dashboard_state.py`,
  `scrapers/{assignment_calendar,batch_results,canvas_page_extractor,google_docs_calendar}.py`,
  `scripts/dashboard_agent.py`) existed only on the server's disk, never committed.
  Now committed. **Lesson:** validate the tracked tree against live process imports,
  not just `pytest` (tests mocked around the gap).
- **`.git` bloat:** history had ~415 MB of unreachable dangling objects (never gc'd).
  A reachability-preserving `git reflog expire --expire=now --all && git gc --prune=now`
  took `.git` 498 MB → 31 MB with zero history rewrite. Do NOT run `filter-repo` on this
  repo's `.git` — it shares its object store with the `personal-assistant-bot-release`
  worktree and would desync it. Rewrite only on an isolated clone.
- **Shared worktree:** `~/personal-assistant-bot-release` is a git *worktree* of the
  same `.git` (branch `fix/2026-07-bug-audit`), not a clone. ~99 dirty files there is
  normal. Never assume it's independent.
- **Empty-string inference results** from the Orange Pi Ollama box — see §4.
- **Test/reality drift:** `tests/test_script_imports.py` hard-codes a module list;
  deleting a script requires updating that list or the suite fails.

---

## 8. Change-safety checklist for an agent

Before committing a change to this repo:

1. `pytest -q` must stay green (baseline 163 passing).
2. `python -c "import main"` (or a fresh-clone import check) must succeed — verifies no
   new untracked runtime dependency.
3. If you touched state I/O: confirm it still goes through `AtomicJSONStore`.
4. If you touched the router: confirm the LOCAL-only-for-sensitive invariant and the
   Ollama-empty retry are intact.
5. If you touched a service entry point: after deploy, verify live with
   `systemctl is-active` + the service's health endpoint. Never trust that a handed-off
   sudo/`!` command actually ran.
6. Never `git add -A` — the working tree carries ~100 legitimately-dirty files; stage
   explicit paths only.
7. Never commit secrets; keep `.env`/`credentials.json`/`token.json` gitignored.
