# pab-core — Personal Assistant Bot

A self-hosted Telegram assistant that helps a high-school student stay on top of
school. It scrapes coursework from Canvas, Google Classroom and GroupMe, turns it
into a periodic digest and study material, answers questions in chat, and keeps a
private calendar of assignments — all while running mostly on local hardware and
keeping personal data off the cloud wherever possible.

This is the **application** repo. Two sibling repos hold the rest of the system:

- **[pab-ops](https://github.com/SanelL112/pab-ops)** — systemd units, health checks, RPC/cluster tooling (infra).
- **pab-study-content** *(private)* — the generated study guides and knowledge base.

---

## What it actually does

- **Runs a Telegram bot** you chat with. It answers questions, summarizes your day,
  and takes quick actions via inline-keyboard buttons.
- **Scrapes your school data** — Canvas (through a real logged-in Firefox/ClassLink
  session, not an API token), Google Classroom/Docs, and a GroupMe class group.
- **Sends a recurring "digest"** (every 4 hours by default) of new assignments,
  announcements and deadlines.
- **Builds study guides** from your coursework and indexes them for semantic search.
- **Keeps a private assignment calendar** you can subscribe to from any CalDAV app.
- **Routes AI work intelligently** — private data goes to local models (Ollama /
  llama.cpp RPC cluster); only non-sensitive work is allowed to touch cloud models.

## The live system (what's running on the server)

The host runs **five systemd services**. This repo provides the code for four of them
(the fifth, `llama-rpc`, is infra defined in `pab-ops`):

| Service | Entry point (in this repo) | Port | Purpose |
|---|---|---|---|
| `bot.service` | `main.py` | — | The Telegram bot (main process) |
| `canvas-browser.service` | `scripts/canvas_browser_daemon.py` | 127.0.0.1:8976 | Persistent logged-in Canvas/ClassLink Firefox session |
| `pab-dashboard-agent.service` | `scripts/dashboard_agent.py` | 0.0.0.0:8765 | Status dashboard agent |
| `assignment-caldav.service` | `radicale` (venv) + external config | 0.0.0.0:5232 | Private CalDAV server for assignments |

> The systemd unit files themselves live in **pab-ops**. This repo holds the Python
> those units execute.

## Quick start (fresh clone)

```bash
git clone https://github.com/SanelL112/pab-core.git
cd pab-core
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure secrets — copy the example and fill it in
cp .env.example .env
$EDITOR .env        # Telegram token, chat id, Notion key, model settings, etc.

# Run the bot
python main.py
```

You need, at minimum, a `TELEGRAM_BOT_TOKEN` and your `TELEGRAM_CHAT_ID` /
`TELEGRAM_OWNER_USER_ID` in `.env`. Canvas scraping additionally needs a working
Firefox profile logged into ClassLink (managed by the canvas-browser daemon).

## Configuration

Everything is driven by environment variables loaded from `.env` via `config.py`.
`config.py` is the single source of truth — it defines every path, model, timeout,
and credential the system uses, with sensible defaults. Notable knobs:

- **Telegram:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_OWNER_USER_ID`
- **AI routing:** `OR_DEFAULT_MODEL`, `OR_FALLBACK_MODEL`, `OR_THIRD_MODEL`
  (OpenRouter tiers), `OLLAMA_LOCAL_URL`, `OLLAMA_ORANGEPI_URL`, `PI_CLASSIFIER_URL`,
  `OPENROUTER_API_KEY`, `HACKCLUB_AI_API_KEY`
- **Local RPC cluster:** `RPC_SURFACE_TIMEOUT`, `RPC_INFERENCE_TIMEOUT`,
  `RPC_FALLBACK_CLOUD_MODEL`, `RPC_FALLBACK_OLLAMA_MODEL`
- **Cadence:** `DIGEST_INTERVAL_SECONDS` (default 14400 = 4h),
  `WATCHDOG_INTERVAL_SECONDS` (default 1800 = 30m)
- **Sources:** `NOTION_API_KEY`, `NOTION_DATABASE_ID`, `GROUPME_TOKEN`,
  `GROUPME_GROUP_ID`, `USE_COMPOSIO`

Secrets are **never committed** — `.env`, `credentials.json`, `token.json`, and
`state.json` are gitignored.

## Telegram commands

`/start` `/help` `/summary` `/model` `/stats` `/ping` `/server` `/errors`
`/canvas` `/calendar` `/classroom` `/correlations` `/priority` `/backup`
`/restore` `/bash`

## Layout

```
main.py                 Telegram bot entry point (bot.service)
config.py               Central configuration (all env-driven settings)
llm_router.py           Unified LLM dispatch: local-first routing + cost tracking
utils.py                Shared helpers: PII scrubbing, backups, correlation, rotation
activity_log.py         Structured, privacy-preserving activity log
ai_processor.py         Per-source local inference passes
nightly_processor.py    Lossless overnight processing of queued study docs
practice_grader.py      Automated practice-test grading
voice_handler.py        Local-only voice transcription
inline_keyboards.py     Quick-action inline keyboards

bot/                    Telegram-facing layer
  commands.py             Command handlers
  ai_bridge.py            Privacy-preserving chat → inference bridge
  ui.py                   Presentation primitives (progress, rendering, escaping)
  state.py                Transactional state management
  storage.py              Durable atomic JSON storage
  security.py             Authorization boundary (owner-only)
  runtime.py              Background task tracking
  dashboard_state.py      Routing state for the status dashboard

scrapers/               Data ingestion + processing ("ingest" layer)
  canvas_scraper.py       Canvas via authenticated Firefox/ClassLink session
  canvas_page_extractor.py Canvas HTML → assignments
  google_scraper.py / composio_fetcher.py  Google sources
  groupme_scraper.py      GroupMe class group
  notion_client.py        Notion task hub
  assignment_calendar.py  Sync assignments → CalDAV + Google Calendar
  google_docs_calendar.py Google Docs deadlines → approval-gated proposals
  morning_digest.py       Digest generation
  mega_study_builder.py   Study-guide builder
  embedding_indexer.py    Semantic vector index from the knowledge corpus
  semantic_retrieval.py   Relevant-chunk retrieval over the index
  batch_results.py        Typed outcomes/validation for unattended batch jobs
  ...

scripts/                Service entry points + helpers run by this repo's daemons
tests/                  pytest suite (163 tests)
docs/archive/           Historical audit/security reports (kept for provenance)
```

## Tests

```bash
source venv/bin/activate
pytest -q          # 163 tests
```

## Design principles

- **Privacy-first / local-first.** Private data is routed to local models; cloud
  models are only used for work explicitly classified as non-sensitive. All logging
  scrubs PII (`utils.scrub_pii`).
- **Owner-only.** Every Telegram handler passes through an authorization boundary
  (`bot/security.py`); only the configured owner can drive the bot.
- **Durability.** State writes are transactional/atomic (`bot/storage.py`,
  `bot/state.py`) so a crash mid-write can't corrupt state.
- **Graceful degradation.** The LLM router has a local→local→cloud fallback chain so
  one dead provider doesn't take the assistant down.

See `ARCHITECTURE.md` for the deep design reasoning and data-flow model.
