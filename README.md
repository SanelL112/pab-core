# pab-core — Personal Assistant Bot

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-160%2B%20passing-brightgreen.svg)]()
[![Privacy](https://img.shields.io/badge/privacy-local--first-success.svg)]()

A self-hosted, privacy-preserving Telegram assistant designed for a student to stay on top of schoolwork, exams, and daily tasks. It automatically scrapes coursework and announcements from Canvas (via persistent ClassLink SSO session), Google Classroom/Docs, and GroupMe, turns them into periodic actionable digests, builds structured study guides, indexes notes for vector retrieval, and maintains an assignment calendar synced to CalDAV and Google Calendar.

---

## 1. System Ecosystem & Repositories

The Personal Assistant Bot ecosystem is architected into three decoupled repositories:

- **[`pab-core`](https://github.com/SanelL112/pab-core)** *(This Repository)*: The core application layer, scrapers, Telegram bot interface, CalDAV sync, and local/cloud LLM routing.
- **[`pab-ops`](https://github.com/SanelL112/pab-ops)**: Host infrastructure, systemd service units, timers, health probes, and multi-node `llama.cpp` RPC cluster operations across local hardware.
- **[`pab-study-content`](https://github.com/SanelL112/pab-study-content)** *(Private)*: Generated Markdown/Word study guides, knowledge base notes, and SAT/ACT prep material produced by the bot pipeline.

---

## 2. Core Capabilities

- **Autonomous Coursework Ingestion**:
  - **Canvas LMS**: Scraped via an authenticated, persistent headless Firefox session (`canvas-browser.service` on port `8976`) maintaining active ClassLink SSO without requiring fragile API tokens.
  - **Google Classroom & Drive**: Pulls documents, assignments, and announcements (`supportsAllDrives=True`).
  - **GroupMe**: Scrapes school group chats for student discussions and homework updates.
  - **Notion**: Syncs tasks and project deadlines to/from Notion workspace.
- **Periodic Smart Digests**: Dispatches a structured summary every 4 hours with new deadlines, unread announcements, and interactive Telegram inline buttons.
- **AI Routing & Privacy Protection**:
  - PII scrubbing (`utils.scrub_pii`) on all outgoing content.
  - Local-first routing hierarchy: Local x86 Ollama → Orange Pi 5 (RK3588) → Distributed `llama.cpp` RPC cluster → Fallback Cloud (OpenRouter / Hack Club AI).
- **Study Guide & Textbook Generation**: Nightly automated document processing (`nightly_processor.py`) that uses OCR, delta appends, and semantic vector indexing (`nomic-embed-text`) for instant retrieval.
- **Calendar & CalDAV Synchronization**: Normalizes assignments into a local Radicale CalDAV server (`0.0.0.0:5232`) and submits approval-gated proposals to Google Calendar.

---

## 3. Host Architecture & Service Topology

The system runs on a host running Debian Linux with the following daemons managed by systemd:

| Service | Entry Point in `pab-core` | Local Port | Role |
| :--- | :--- | :--- | :--- |
| `bot.service` | `main.py` | — | Telegram Bot long-polling daemon & scheduled jobs |
| `canvas-browser.service` | `scripts/canvas_browser_daemon.py` | `127.0.0.1:8976` | Persistent ClassLink/Canvas authenticated Firefox session |
| `pab-dashboard-agent.service` | `scripts/dashboard_agent.py` | `0.0.0.0:8765` | Web status dashboard endpoint |
| `assignment-caldav.service` | `radicale` (venv) | `0.0.0.0:5232` | Private CalDAV server for calendar subscriptions |
| `llama-rpc.service` | *(Defined in `pab-ops`)* | `0.0.0.0:8080` | Distributed `llama.cpp` local inference cluster |

---

## 4. Quickstart & Installation

### Prerequisites
- Python 3.11+
- Git & Virtualenv
- Firefox & geckodriver (for Canvas scraping daemon)
- Local Ollama daemon or OpenRouter API key

### Setup Steps
```bash
# Clone the repository
git clone https://github.com/SanelL112/pab-core.git
cd pab-core

# Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
$EDITOR .env   # Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_USER_ID, etc.

# Run unit and integration tests
pytest -q

# Launch the bot
python main.py
```

---

## 5. Environment Variables & Configuration

Configuration is managed centrally in `config.py` using `.env`:

| Variable | Description | Default / Example |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Bot API token from `@BotFather` | `123456789:ABC...` |
| `TELEGRAM_OWNER_USER_ID` | Telegram User ID of authorized owner | `123456789` |
| `TELEGRAM_CHAT_ID` | Default chat ID for notifications & digests | `123456789` |
| `OLLAMA_LOCAL_URL` | Local Ollama inference URL | `http://127.0.0.1:11434` |
| `OLLAMA_ORANGEPI_URL` | Orange Pi 5 Ollama endpoint | `http://10.10.10.2:11434` |
| `PI_CLASSIFIER_URL` | Orange Pi concurrent classifier endpoint | `http://10.10.10.2:8080` |
| `OPENROUTER_API_KEY` | OpenRouter API Key for cloud fallback | `sk-or-v1-...` |
| `OR_DEFAULT_MODEL` | Primary OpenRouter model | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `DIGEST_INTERVAL_SECONDS` | Interval between scheduled digest checks | `14400` (4 hours) |
| `NOTION_API_KEY` | Notion Integration Token | `secret_...` |
| `NOTION_DATABASE_ID` | Target Notion Task Database ID | `32-char hex string` |
| `GROUPME_TOKEN` | GroupMe API access token | `...` |

---

## 6. Telegram Commands

| Command | Action |
| :--- | :--- |
| `/start` | Welcome message, health status check, and interactive keyboard |
| `/summary` | Trigger on-demand generation and delivery of school digest |
| `/canvas` | Check status of Canvas scraper and recent assignments |
| `/classroom` | Fetch recent Google Classroom coursework and updates |
| `/calendar` | Display upcoming deadlines and sync status with CalDAV |
| `/model` | Query or toggle active LLM routing tier |
| `/stats` | View inference token counts, cache stats, and cost breakdown |
| `/errors` | Display recent error log excerpts and dead-letter queue count |
| `/backup` | Create snapshot backup of `state.json` and local indices |
| `/bash` | *(Owner-only)* Execute sandboxed diagnostics on host |

---

## 7. Repository Layout

```
pab-core/
├── main.py                     # Main bot process and Telegram event loop
├── config.py                   # Central settings, environment loader & defaults
├── llm_router.py               # Local-first LLM router with cost/token tracking
├── ai_processor.py             # Local model extraction and summarization
├── utils.py                    # PII scrubbing, atomic backups, rotation utilities
├── activity_log.py             # Privacy-preserving audit logging
├── nightly_processor.py        # Overnight document processing & index updates
├── practice_grader.py          # SAT/ACT exam scoring and evaluation
│
├── bot/                        # Bot Telegram Presentation Layer
│   ├── commands.py             # Slash command dispatchers
│   ├── ai_bridge.py            # Chat-to-model bridge with context enrichment
│   ├── ui.py                   # Telegram HTML escaping & formatting
│   ├── state.py                # Transactional state manager
│   ├── storage.py              # Atomic JSON read/write with locking
│   ├── security.py             # Strict owner authorization perimeter
│   └── runtime.py              # Background job lifecycle management
│
├── scrapers/                   # Data Ingest & Scraper Tier
│   ├── canvas_scraper.py       # Canvas scraper via browser daemon
│   ├── canvas_page_extractor.py# HTML extractor for assignments
│   ├── google_scraper.py       # Google Classroom / Drive API client
│   ├── groupme_scraper.py      # GroupMe class chat reader
│   ├── notion_client.py        # Notion workspace integration
│   ├── assignment_calendar.py  # Radicale CalDAV + Google Calendar syncer
│   ├── google_docs_calendar.py # Approval-gated Google Docs deadline extractor
│   ├── morning_digest.py       # 4-hour digest compiler
│   ├── mega_study_builder.py   # Multi-stage textbook compiler
│   ├── embedding_indexer.py    # Incremental vector index builder
│   └── semantic_retrieval.py   # Vector similarity chunk retrieval
│
├── scripts/                    # Daemons & CLI Utilities
│   ├── canvas_browser_daemon.py# Headless Firefox ClassLink SSO session daemon
│   └── dashboard_agent.py      # Web status dashboard service
│
└── tests/                      # Automated test suite (pytest)
```

---

## 8. Testing & Validation

Run the full automated test suite:
```bash
source venv/bin/activate
pytest -v
```

To run a specific test category:
```bash
pytest tests/test_assignment_calendar.py
pytest tests/test_main_fixes.py
```

---

## 9. Security & Invariants

- **Secrets Handling**: `.env`, `credentials.json`, `token.json`, and `state.json` are excluded via `.gitignore`. Never commit credentials.
- **Privacy Protection**: Cloud LLM requests must never include raw personal names, addresses, student IDs, or school credentials. All text sent externally is scrubbed by `utils.scrub_pii()`.
- **Atomic State Operations**: Writes to `state.json` or `.nightly_queue.json` use file locks and temp-write-then-rename semantics to prevent corruption during unexpected shutdowns.
