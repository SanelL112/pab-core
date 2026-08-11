# AI Developer Context

**If you are an AI Agent reading this file**, welcome to the Personal Assistant Bot codebase! The user has asked you to help maintain, debug, or extend this system. This file provides the critical architectural context you need to safely edit this project.

## Core Philosophy
1. **Hybrid LLM Architecture**: This system uses OpenRouter (default: `nvidia/nemotron-3-ultra-550b-a55b:free`, fallback: `nvidia/nemotron-3-nano-30b-a3b:free`) and local agy (`flash`/`pro`) for heavy reasoning and study guide generation, falling back to local processing when necessary. Always ensure `OPENROUTER_API_KEY` is utilized for intense tasks.
2. **Syncthing Pipeline**: All generated Markdown files meant for the user MUST be deposited exactly into `/home/sanel/personal-assistant-bot/study_guides/`. Do not pollute the root directory. Syncthing is mapped strictly to `study_guides/` to natively beam files into Obsidian.
3. **Token Conservation (Delta Updates)**: Do not wipe and rewrite 400KB study guides from scratch. We use lightweight append-only loops in `memory_consolidation.py` to add new daily classroom notes to the bottom of existing guides.
4. **No Direct Database**: This system uses raw markdown files (`mega_index.md`, `curated_brain.md`) and JSON files as its database.

## Codebase Map

### 1. `main.py`
The entry point. It hosts the `python-telegram-bot` instance and sets up the internal task scheduler (`JobQueue`).
- Handles incoming messages, images, and voice notes.
- Uses `ai_processor.py` to route queries.
- Contains the `morning_digest` (runs daily at 7:00 AM).

### 2. `scrapers/` Directory (The Engine)
This is where all data ingestion occurs.
- `google_scraper.py`: Fetches Google Docs, Classroom assignments, and Gmail. Critical logic: `download_drive_file` natively exports Google Docs to PDF.
- `canvas_scraper.py`: Hooks into the Canvas API.
- `nightly_processor.py`: Triggers at 2:00 AM to execute the OCR pipeline (`extract_notes.py`) and dynamically rebuild or update the SAT study guides using the Token-Saving Append logic.
- `mega_study_builder.py`: A sprawling, multi-stage Editor-in-Chief script that scrapes YouTube and Web Articles to build 50-page textbooks dynamically.

### 3. Knowledge Files (The Brain)
- `mega_index.md`: A summarized catalog of the user's data.
- `curated_brain.md`: An exhaustive, append-only timeline of everything the bot sees or does.
- `bot_context.txt`: A tightly compressed text file injected into the bot's real-time prompt to give it memory.

### 4. Semantic Retrieval System (NEW)
- `scrapers/embedding_indexer.py`: Builds a vector index from all knowledge sources using Ollama's `nomic-embed-text` model. Runs as Phase 5 of the nightly pipeline (1 AM). Produces `embedding_data/embedding_index.npz` with numpy vectors + chunk text + source paths.
- `scrapers/semantic_retrieval.py`: At query time, embeds the user's message via Ollama and finds the top-K most relevant chunks by cosine similarity. Falls back to old tail-truncation if the index doesn't exist or Ollama is down.
- `main.py` `send_to_antigravity_and_wait()`: Now calls `semantic_retrieval.get_context_for_prompt()` first. If semantic retrieval returns results, it replaces the old `brain_context` + `digest_context` tail-read. Otherwise falls back to `get_fallback_context()`.
- The index is incremental: only re-embeds sources whose MD5 has changed. Full rebuild takes ~35 minutes on the i5 CPU.

## Common Pitfalls & Rules for Editing
- **Server Environment**: The user runs this on a Debian server. `bot.service` manages the daemon.
- **Google Drive Permissions**: If you query Google Drive, ALWAYS use `supportsAllDrives=True` and `corpora="allDrives"`.
- **Syncthing Ghost Files**: Do not generate temporary `.log`, `.jpg`, or `.txt` cache files inside the `study_guides/` folder, or Syncthing will upload them to the user's Obsidian Vault. Save them in `source_cache/` instead.

