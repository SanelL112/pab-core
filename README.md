# Personal Assistant Bot (Local AI Ecosystem)

Welcome to the Personal Assistant Bot! This is a highly integrated, intelligent assistant designed to act as an autonomous ecosystem for a home server environment. It seamlessly manages academic data, serves as a continuous knowledge indexer, and provides robust personal assistance directly through Telegram.

## Core Features

- **Telegram Interface**: Interact with the bot natively through Telegram. Ask questions, run commands, upload images for OCR and homework help, and receive push notifications or daily digests.
- **3-Tier Security Model for LLM Routing**: A strict privacy-first pipeline ensures sensitive personal data is protected:
  - **PII Filter**: Every message is scanned by a local `agy flash` model to detect Personally Identifiable Information (PII).
  - **Local Processing**: Any message containing PII is strictly routed to local LLMs (e.g., Ollama Qwen/Llama, Agy) and never touches the cloud.
  - **Cloud Processing (OpenRouter)**: Safe, non-sensitive complex queries or academic tasks are routed to OpenRouter models (like Llama 3.3 70B or Nemotron 3 Ultra) for powerful reasoning.
- **Semantic Retrieval System**: Uses Ollama (`nomic-embed-text`) to build a semantic vector index from all your knowledge sources (study guides, historical data, classroom PDFs). At query time, the bot retrieves the most relevant context using cosine similarity, giving it a deep "memory" of your specific content.
- **Continuous Knowledge Indexing**: Automatically scrapes data from Google Classroom, Canvas, Google Docs, and Gmail. It can process downloaded Classroom PDFs and OCR handwritten notes to weave them into your overall knowledge base.
- **Syncthing & Obsidian Integration**: Completely integrated with Syncthing. All generated master study guides and knowledge logs are explicitly saved into a dedicated `study_guides/` folder, which syncs directly into your local Obsidian Vault.
- **Nightly Delta-Updates**: Every night, the bot runs a pipeline to append new knowledge and daily school topics to your existing massive `.md` textbooks. This append-only "delta" logic prevents token waste and avoids rebuilding huge guides from scratch.

## Architecture & Ecosystem

The system operates across several distinct, asynchronous pipelines:

1. **The Telegram Hub (`main.py`)**: The central entry point for the bot. It hosts the `python-telegram-bot` instance, handles commands, routes incoming messages through the PII filter, and manages photo/voice inputs.
2. **The LLM Router (`llm_router.py`)**: A unified interface that determines whether a prompt should go to a local Ollama instance, a local Agy model, or out to OpenRouter, ensuring the privacy rules are followed.
3. **The Scrapers (`scrapers/`)**:
   - `google_scraper.py` & `canvas_scraper.py`: Fetches documents, assignments, emails, and announcements.
   - `extract_notes.py`: The OCR pipeline for decoding handwritten notes and downloaded PDFs.
4. **The Nightly Brain (`scrapers/memory_consolidation.py` & `nightly_processor.py`)**: Runs offline in the early hours (e.g., 1:00 AM / 2:00 AM) to process the day's scraped data, update the `curated_brain.md`, run the OCR pipeline, and build the semantic vector index.
5. **Mega Study Builder (`mega_study_builder.py`)**: A multi-stage pipeline that builds comprehensive, chapter-based textbooks dynamically using web sources and transcripts.

## Data Sources (The Brain)

The system doesn't rely on a traditional database. Instead, it uses markdown and JSON files:
- `knowledge_base/`: Core subject guides.
- `study_guides/`: Auto-generated ACT/SAT study guides and the `curated_brain.md` file (which syncs to Obsidian).
- `mega_index.md`: A historical catalog of user data.
- `scrapers/source_cache/`: Temporary storage for daily scraped digests.
- `embedding_data/`: The semantic vector index (`embedding_index.npz`) built by Ollama.

## Setup Instructions

1. **Prerequisites**:
   - Python 3.10+
   - Tesseract OCR (`sudo apt install tesseract-ocr`)
   - [Ollama](https://ollama.com/) (Must be installed and running on port 11434)
   - Syncthing configured to point to `/home/sanel/personal-assistant-bot/study_guides/`

2. **Environment Variables**:
   Create a `.env` file in the root directory:
   ```env
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token
   OPENROUTER_API_KEY=your_openrouter_token
   # Canvas uses the normal ClassLink session in a persistent Firefox profile.
   CANVAS_API_URL=https://your-school.instructure.com
   CANVAS_SSO_ENTRY_URL=https://launchpad.classlink.com/forsyth
   CLASSLINK_USERNAME=your_classlink_username
   CLASSLINK_PASSWORD=your_classlink_password
   ```

   Canvas uses one persistent Firefox session rather than a copied browser cookie. With the
   Firefox desktop available through VNC, start `scripts/canvas_browser_daemon.py` with
   `DISPLAY=:1`, sign into ClassLink manually, and leave that browser daemon running.

3. **Google API Credentials**:
   Place your `credentials.json` in the root directory to generate a `token.json` file for Google Workspace integration (Classroom, Drive, Gmail).

4. **Running the Bot**:
   The bot is designed to run continuously on a server, typically managed as a systemd service (`bot.service`). Ollama must be running in the background for embedding and local queries.

## Server Constraints & Considerations

- **Hardware**: Designed for a home server environment (e.g., i5 CPU, ~6GB RAM). Heavy ML tasks (like embedding with Ollama) are run on the CPU and may take time, which is why intensive tasks are scheduled as nightly batch jobs.
- **Data Privacy**: Always ensure the local routing logic in `main.py` and `llm_router.py` remains intact to prevent accidental PII leakage to cloud providers.
- **Syncthing Pipeline**: Temporary files or cache data must be saved to `scrapers/source_cache/` rather than `study_guides/` to prevent cluttering the user's Obsidian Vault.
