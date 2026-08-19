# Bug audit verification report

- Source doc: CONSOLIDATED_BUG_AUDIT_AND_REMEDIATION.md (1194 lines)
- Findings in doc: 40
- Method: static source verification, safe filesystem checks, py_compile warning check, redacted journald count, live Canvas current-user probe from this Hermes session.

## Existing audit findings

| ID | Verdict | Confidence | Evidence summary |
|---|---|---|---|
| SEC-01 | EXISTS | HIGH | utils.py: ALLOWED_COMMAND_TEMPLATES includes python3 -c; utils.py: Python check only blocks specific substrings: True; bot/ai_bridge.py: prompt mentions python3 -c: True |
| SEC-02 | EXISTS | HIGH | main.py start decorators before function: ['', '', '# ── Telegram handlers ──────────────────────────────────────────────────────────']; start has @require_auth: False; start schedules job_queue.run_repeating: True |
| SEC-03 | EXISTS | HIGH | main.py:12:from utils import scrub_pii; main.py:246:from llm_router import call_local_rpc, call_openrouter; main.py:260:result = call_openrouter( |
| SEC-04 | EXISTS | HIGH | journalctl token-shaped Bot API URL count (redacted, not printed): 83734 |
| SEC-05 | EXISTS | HIGH | bot/ai_bridge.py:31:existing_files = glob.glob(os.path.join(history_dir, f"chat_history_{chat_id}_*.txt")); bot/ai_bridge.py:36:m = re.search(f"chat_history_{chat_id}_(.+)\\.txt", basename); bot/ai_bridge.py:99:logger.info(f"PII detected ({pii_str}) — routing entirely via Pi Ollama") |
| LLM-01 | EXISTS | MEDIUM | .env OLLAMA lines: OLLAMA_URL="http://10.10.10.2:11434"; config.py default OLLAMA_URL lines: ['OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")'] |
| LLM-02 | EXISTS | HIGH | call_local_rpc mentions Surface/LLAMACPP: True; call_local_rpc mentions Pi/Ollama: True; call_local_rpc explicit Dell/localhost fallback: False |
| LLM-03 | EXISTS | HIGH | semantic_retrieval uses config OLLAMA_URL: True; semantic_retrieval still starts `ollama serve`: True; readiness checks same configured URL after spawn: True |
| LLM-04 | EXISTS | HIGH | ollama-health-check.sh localhost checks: True; bot-health-check.sh localhost/local model checks: True; app .env OLLAMA_URL line: ['OLLAMA_URL="http://10.10.10.2:11434"'] |
| LLM-05 | EXISTS | MEDIUM | llm_router.py:41:# Configure timeouts: connect=10s, read=60s, write=10s, pool=5s; llm_router.py:42:timeout = httpx.Timeout(10.0, connect=10.0, read=60.0, write=10.0, pool=5.0); llm_router.py:44:timeout=timeout, |
| LLM-06 | EXISTS | HIGH | provider signatures: call_opencode(prompt first)=True; call_hackclub(prompt first)=True; bot/ai_bridge.py:386:lambda: call_opencode("hy3-free", full_prompt, task=f"chat-{topic}", timeout=RESPONSE_TIMEOUT); bot/ai_bridge.py:399:lambda: call_hackclub("qwen/qwen3-32b", full_prompt, task=f"chat-{topic}", timeout=RESPONSE_TIMEOUT) |
| LLM-07 | EXISTS | HIGH | bot/ai_bridge.py:507:# Lightweight sanity check: only run on responses that look suspicious; bot/ai_bridge.py:509:_suspicious = (; bot/ai_bridge.py:510:len(out.strip()) < 20 |
| MCP-01 | EXISTS | HIGH | Live Composio CANVAS_GET_CURRENT_USER returned 401 Expired access token, expired_at 2026-07-18T00:00:00Z in this session. |
| MCP-02 | EXISTS | HIGH | scrapers/composio_fetcher.py:52:Returns the response data dict (successful: bool, data: {...}).; scrapers/composio_fetcher.py:56:return {"successful": False, "data": {"message": "Composio token not available"}}; scrapers/composio_fetcher.py:87:# Parse SSE-style response (data: {...} lines) |
| MCP-03 | EXISTS | HIGH | main.py has USE_COMPOSIO=True: True; bot-health-check.sh checks token.json: True; scripts/bot-health-check.sh:76:# Google scrapers |
| MCP-04 | HISTORICAL/NOT_CODE_BUG | MEDIUM | No current code defect to verify; audit says Hermes MCP reconnects recovered. |
| LOG-01 | EXISTS | HIGH | setup_telegram_logging before logging.basicConfig in main top section: True |
| LOG-02 | EXISTS | HIGH | telegram_logger.py:35:def emit(self, record):; telegram_logger.py:64:requests.post(url, json=payload, timeout=2); telegram_logger.py:65:except Exception: |
| LOG-03 | EXISTS | HIGH | log_scanner.py:127:since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S"); log_scanner.py:138:"timestamp": line[:25].strip(),; log_scanner.py:146:"timestamp": datetime.now().isoformat(), |
| LOG-04 | EXISTS | HIGH | scripts/bot-health-check.sh:5:# Logs: journalctl -u bot-health.service; scripts/bot-health-check.sh:58:ERROR_COUNT=$( { journalctl -u bot.service --no-pager --since "4 hours ago" 2>/dev/null || true; } | { grep -icE "error|fail|traceback" || true; } | tr -d ' '); scripts/bot-health-check.sh:63:SUMMARIES_KB=$(du -k "$BOT_DIR/source_cache/combined_summaries.txt" 2>/dev/null | cut -f1 || echo "0") |
| ASYNC-01 | EXISTS | HIGH | main.py:260:result = call_openrouter(; main.py:584:transcription = transcribe_voice(tmp_path); main.py:698:extracted = call_openrouter( |
| ASYNC-02 | EXISTS | HIGH | main.py:922:job_queue.run_repeating(lambda ctx: enforce_all_rotations(), interval=21600, first=21600, chat_id=SANEL_CHAT_ID, name="rotation_enforcement"); main.py:926:lambda ctx: create_backup(), |
| ASYNC-03 | EXISTS | HIGH | utils.py:15:import atexit; utils.py:865:def get_async_httpx_client() -> httpx.AsyncClient:; utils.py:871:_cached_sessions['httpx_async'] = httpx.AsyncClient( |
| MEDIA-01 | EXISTS | HIGH | main.py:202:with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:; main.py:233:os.unlink(tmp_path); main.py:530:reply = await send_to_antigravity_and_wait(user_text, chat_id, context, thinking_msg) |
| MEDIA-02 | EXISTS | HIGH | main.py:28:from voice_handler import transcribe_voice; main.py:202:with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:; main.py:233:os.unlink(tmp_path) |
| HANDLER-01 | EXISTS | MEDIUM | main.py:154:except Exception as e:; main.py:302:except Exception as e:; main.py:342:except Exception as e: |
| STATE-01 | EXISTS | HIGH | bot/state.py:18:_state_lock = threading.Lock(); bot/state.py:41:def load_state() -> dict:; bot/state.py:43:with _state_lock: |
| NOTION-01 | EXISTS | HIGH | main.py:119:from bot.state import load_state, save_state, is_sleep_window, get_hash; main.py:187:if thash not in state.setdefault("seen_tasks", []):; main.py:188:state["seen_tasks"].append(thash) |
| DATA-01 | EXISTS | HIGH | cache: exists=True count=20; source_cache: exists=True count=7; scrapers/source_cache: exists=True count=9 |
| DATA-02 | EXISTS | HIGH | root nightly_queue exists/count: True/280; scrapers/nightly_queue exists/count: True/0; scrapers/nightly_processor.py:13:queue_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nightly_queue.json") |
| DATA-03 | EXISTS | HIGH | ai_processor.py:224:from utils import has_changed; ai_processor.py:225:if not has_changed(name, data[:1000]):; ai_processor.py:290:from utils import mark_processed as _mark_processed |
| DATA-04 | EXISTS | MEDIUM | root nightly_processor.py __main__ occurrences: 2; pkill present: True; git pull/commit/push present: True |
| DATA-05 | EXISTS | MEDIUM | scrapers/google_scraper.py:78:except Exception as e:; scrapers/google_scraper.py:136:except Exception as e:; scrapers/google_scraper.py:176:except Exception: |
| DEP-01 | EXISTS | HIGH | scrapers/mega_study_builder.py:6:from duckduckgo_search import DDGS; scrapers/mega_study_builder.py:8:from youtube_transcript_api import YouTubeTranscriptApi; scrapers/mega_study_builder.py:104:ytt_api = YouTubeTranscriptApi()  # New instance-based API (v1.0+) |
| DEP-02 | EXISTS | HIGH | httpx occurrences in requirements.txt: 2; unpinned sample: python-telegram-bot[job-queue], python-dotenv, canvasapi, google-api-python-client, google-auth-httplib2, google-auth-oauthlib, httpx, requests, pytesseract, Pillow, numpy, python-docx |
| UI-01 | EXISTS | HIGH | inline_keyboards.py:23:InlineKeyboardButton(f"� {tid} - Low", callback_data=f"task_prio:{tid}:low"),; inline_keyboards.py:41:InlineKeyboardButton(f"� Build Guide: {topic}", callback_data=f"build_guide:{topic}"),; inline_keyboards.py:58:InlineKeyboardButton("� Schedule Study Time", callback_data=f"schedule_guide:{guide_name}"), |
| WARN-01 | EXISTS | HIGH | patch_utils.py:76: SyntaxWarning: invalid escape sequence '\s'   r'rm\s+-rf\s+/', r'dd\s+if=', r':\(\)\{', r'fork bomb', scripts/telegram_notify.py:102: SyntaxWarning: invalid escape sequence '\|'   today_7am_out, _ = run_cmd('journalctl -u bot.service --since "today 07:00" --until "today 07:10" 2>/dev/null | grep -i "digest\|morning\|send_morning"') |
| TEST-01 | EXISTS | HIGH | scratch/test_notion.py; scratch/test_exists.py; comprehensive_test.py:1:import sys |
| TEST-02 | EXISTS | MEDIUM | TELEGRAM_CHAT_ID zero in workflow: True; import-check catches generic Exception and skips: True; no assertion skipped==0: True |
| TEST-03 | EXISTS | HIGH | pytest mentioned in workflow: False; pyflakes mentioned: True; workflow job name: lint |

## Missed bug candidates found

### MISS-02 — Shell=True/os.system call sites need command-injection review (HIGH)
- utils.py:298:SECURITY: Uses allowlist validation, NO shell=True, arguments passed as list.
- utils.py:318:# Parse command into args (NO shell=True)
- utils.py:327:shell=False,  # CRITICAL: No shell=True
- fix_bot_commands.py:9:'subprocess.check_output("uptime", shell=True, text=True)',
- fix_bot_commands.py:16:res = subprocess.check_output("systemctl is-active minecraft || echo 'inactive'", shell=True, text=True).strip()
- fix_bot_commands.py:32:res = subprocess.check_output("tail -n 10 /tmp/embed_build4.log || echo 'No log found'", shell=True, text=True).strip()
- fix_bot_commands.py:51:res = subprocess.check_output("systemctl status antigravity-bot | head -n 5", shell=True, text=True).strip()
- fix_bot_commands.py:64:'subprocess.check_output("journalctl -u minecraft -n 10 --no-pager", shell=True, text=True)',
- fix_bot_commands.py:70:'subprocess.check_output("free -h", shell=True, text=True)',
- fix_bot_commands.py:77:res = subprocess.check_output("systemctl list-units --type=service --state=running | head -n 10", shell=True, text=True).strip()
- fix_bot_commands.py:90:'subprocess.check_output("journalctl -u antigravity-bot -n 10 --no-pager", shell=True, text=True)',
- fix_bot_commands.py:96:'subprocess.check_output("sudo systemctl start minecraft", shell=True)',
- fix_bot_commands.py:102:'subprocess.check_output("sudo systemctl stop minecraft", shell=True)',
- scripts/telegram_notify.py:50:result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)

### MISS-03 — Direct/OpenRouter credential references outside central router (HIGH)
- generate_mega_guide.py:49:OPENROUTER_API_KEY,
- generate_mega_guide.py:375:if not OPENROUTER_API_KEY:
- generate_mega_guide.py:376:return "❌ Missing OPENROUTER_API_KEY"
- scrapers/mega_study_builder.py:225:from config import OPENROUTER_API_KEY
- scrapers/mega_study_builder.py:227:if not OPENROUTER_API_KEY:
- scrapers/mega_study_builder.py:228:return "❌ Missing OPENROUTER_API_KEY in .env"
- scrapers/web_precacher.py:39:api_key = os.getenv("OPENROUTER_API_KEY")
- scrapers/web_precacher.py:41:logger.warning("No OPENROUTER_API_KEY found, aborting web precache.")
- bot/ai_bridge.py:285:"https://openrouter.ai/api/v1/chat/completions",
- bot/ai_bridge.py:287:"Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",

### MISS-05 — Duplicate function definitions in same file can shadow earlier code (MEDIUM)
- main.py: ['_safe_scrape']
- comprehensive_test.py: ['__init__']
- bot/commands.py: ['wait_for_future']

### MISS-06 — Unsafe eval/exec/pickle/yaml.load patterns (HIGH)
- utils.py:190:'__import__', 'importlib', 'exec(', 'eval(', 'os.system',
- utils.py:246:dangerous = ['os.system', 'eval(', 'exec(', '__import__', 'open(', 'importlib']

### MISS-07 — Additional Telegram handlers registered without auth decorator (CRITICAL)
- main.py:474: async handler start registered without @require_auth

### MISS-08 — asyncio.create_task call sites may be untracked/cancellation-unsafe (MEDIUM)
- bot/commands.py:273:result = await _track_task(asyncio.create_task(wait_for_future()))
- bot/commands.py:333:result = await _track_task(asyncio.create_task(wait_for_future()))
- bot/ai_bridge.py:630:_track_task(asyncio.create_task(_run_verification_bg(summary_prompt, chat_id)))

### MISS-09 — User/callback-influenced file path operations need traversal validation (HIGH)
- generate_mega_guide.py:463:with open(file_path, "rb") as f:
- scrapers/web_precacher.py:139:with open(os.path.join(db_dir, filename), "w") as f:
