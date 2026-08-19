# Personal-Assistant-Bot → Multiple GitHub Repos — Split Plan

> **⏸️ RESUME STATE (2026-08-11, updated ~23:30) — read this first**
> - **Phase 0: DONE.** git-filter-repo installed; git identity = SanelL112 / sanel.lathiya@gmail.com; backup `~/pab-presplit-20260811.tar.gz` (615M); baseline 166 passed, 4 services active, 251 tracked files.
> - **Phase 1: pab-study-content PUSHED & VERIFIED.** Private repo `github.com/SanelL112/pab-study-content`, HEAD a65ffa6, **115 files / 62 commits**, file-list matches staging exactly, secrets-clean (tree+history). Verified via independent re-clone. Staging copy at `~/pab-study-content-staging`. (Earlier wrong-branch push was fixed by delete+recreate+`push HEAD:main`.)
> - **Phase 1 REMAINING:** untrack the content dirs from the LIVE monorepo (`git rm -r --cached study_guides knowledge_base offline_archive; git rm --cached '*.docx' mega_index.md`; extend .gitignore; keep on disk). NOT done yet — awaiting go-ahead. Live monorepo still 100% intact.
> - **NEXT:** Phase 2 (pab-ops, PUBLIC, filter-repo on fresh /tmp clone) → Phase 3 (rename existing repo → pab-core PUBLIC, dead-code cleanup, safe `git gc` shrink 495M→~50-80M).
> - **LESSON:** never `git branch -m main` when a `main` already exists in a clone (silent conflict) and never chain it behind `2>/dev/null`. Delete stray branches first, then rename, and always `git push origin HEAD:main` + verify by re-clone. Token in `~/.hermes/.env`; GITHUB_USER=SanelL112.

# (original plan below)

> **For Hermes:** This supersedes the earlier single-repo reorg plan. Goal is now **genuinely separate GitHub repositories**, not just subpackages. Execute phase-by-phase; the coupling analysis below dictates that a naive "one repo per layer" is NOT achievable until import cycles are broken first. Read the "Hard reality" section before touching anything.

**Goal:** Split the cluttered `~/personal-assistant-bot` monorepo into multiple standalone GitHub repositories, each independently cloneable/buildable, without breaking the live bot, the 4 systemd units, or the shared-`.git` `-release/` worktree.

**Tech Stack:** Python 3, python-telegram-bot, httpx/requests, pytest, git-filter-repo (now installed), git worktrees, systemd. `gh` CLI is **NOT installed**; use `git` + `curl` with `GITHUB_TOKEN` sourced from `~/.hermes/.env`.

---

## LOCKED DECISIONS (2026-08-11)

1. **Repo count:** ONE split boundary set → **3 repos** (`pab-core`, `pab-ops`, `pab-study-content`). No 6-repo per-layer split (import cycles forbid it).
2. **Content repo:** **history-preserving** (git-filter-repo on isolated clone), **private**.
3. **Visibility:** `pab-core` **public**, `pab-ops` **public**, `pab-study-content` **private** (the only private one — it holds study/exam data).
4. **pab-core:** rename existing `Antigravity-Based-Assistant-Bot` → `pab-core` in place (3a).
5. **git identity:** `user.name="SanelL12"`, `user.email="sanel.lathiya@gmail.com"`.
   > NOTE: GitHub repo OWNER in API paths stays `SanelL112` (the actual remote owner) — "SanelL12" is only the commit author name.
6. **`.git` shrink:** MEASURED — see below. `.git` = 495 MB but only **~80 MB is reachable**; **~415 MB is unreachable loose objects** (never gc'd; 2662 loose, in-pack:16). Plan: run **worktree-safe `git gc --prune=now`** on pab-core (495 MB → ~50-80 MB, ZERO history rewrite, safe for the shared worktree). Skip the risky filter-repo rewrite of pab-core history unless Sanel later wants ~80→~20 MB.
   - Repeat offenders in reachable history: `embedding_data/embedding_index.npz` (6 copies, ~12 MB), `pull_models.log` (3.3 MB), `source_cache/combined_summaries.txt` (multiple ~1 MB).
7. **Token location:** `GITHUB_TOKEN` line added to `~/.hermes/.env` by Sanel (scope: `repo`). Pushes already work via `~/.git-credentials` + `credential.helper=store`; token var is only needed for the create/rename `curl` calls.

---

## Hard reality: what the code coupling allows (measured 2026-08-11)

Before deciding *how many* repos, here is the actual dependency truth from the import graph:

- **`config.py` is imported by ~35 modules** across every layer. It is the universal keystone.
- **There are CIRCULAR imports.** The "shared core" imports *back into* the bot package:
  - `utils.py:33` → `from bot.state import update_state`
  - `llm_router.py:267,610` → `from bot.dashboard_state import record_route`, `from bot.ui import render_assistant_text`
  - `scrapers/composio_fetcher.py:24` & `scrapers/nightly_processor.py:19` → `from bot.storage import AtomicJSONStore`
- **Consequence:** You cannot put `utils`/`llm_router` in a "shared library" repo and `bot/` in a separate repo, because they import each other. **Two separate repos with a mutual import cannot both `pip install` cleanly.** The cycle must be broken first (move `bot.storage`, `bot.state.update_state`, `bot.ui.render_assistant_text`, `bot.dashboard_state.record_route` into the shared core), or those pieces must live together in ONE repo.

**Therefore the realistic split is 3 repos, not 6:**

| Repo | Contents | Why it can stand alone |
|------|----------|------------------------|
| **`pab-core`** (the bot) | config, utils, llm_router, activity_log, telegram_logger, send_telegram, inline_keyboards, voice_handler, ai_processor, practice_grader, overnight_researcher, study_companion, nightly_processor, `bot/` package, `scrapers/` (ingest), `main.py`, tests | These are mutually entangled (cycles + shared config). They must ship together. This is the live bot. |
| **`pab-ops`** | `scripts/` shell + systemd units + `surface/` cluster tooling + health checks + rpc runbooks + pull_models.sh | Pure infra. The `.sh`/`.service`/`.timer`/`.plist` files have **zero Python coupling** to the bot. 3 python scripts (`canvas_browser_daemon`, `canvas_new_course_watch`, `generate_daily_digest`, `telegram_notify`) DO import `config` → keep those in pab-core OR give pab-ops a thin `config` shim. Truly standalone: `notion_backfill_metadata.py`, `pi_classifier_server.py`, `cluster_manager.py`. |
| **`pab-study-content`** | `study_guides/`, `knowledge_base/`, root `*.docx`, `mega_index.md`, `offline_archive/` — the generated study artifacts | This is **data, not code**. It is what bloats `.git` to 498M. It belongs in its own content repo (or Git-LFS / release assets), NOT in the code repo. No import coupling at all — the cleanest true split. |

Everything else (`embedding_data/`, `backups/`, `state.json`, `.env`, `logs/`, one-off `fix_*.py`, `*.bak`, stale audit `.md`) is **untracked/deleted, not split into a repo**.

---

## Current Context / Assumptions

- **Tooling:** `git 2.47.3` (subtree available). `gh`: **NOT installed**. `git-filter-repo`: **NOT installed** (needs `pip install git-filter-repo` — no sudo required, installs to user site). Auth: token in `~/.git-credentials` for `github.com`, `credential.helper=store`.
- **git identity is UNSET globally** (`user.name`/`user.email` empty) → must set before any commit in a fresh clone, or commits fail/attribute wrong.
- **Existing remote:** `origin = https://github.com/SanelL112/Antigravity-Based-Assistant-Bot.git`. Owner = `SanelL112`.
- **`.git` = 498M, working tree = 1.2G, tracked source only 18M.** History bloat is committed `.docx`/`.npz`/`.tar.gz`.
- **Worktree (CRITICAL):** `~/personal-assistant-bot-release` shares this `.git` (branch `fix/2026-07-bug-audit`). **Any `git filter-repo`/`filter-branch`/BFG on the primary `.git` will desync/corrupt the worktree.** All history-rewriting MUST happen on an **isolated fresh clone**, never on `~/personal-assistant-bot` itself.
- **4 live systemd units** pin the repo path: `bot.service` (+`release.conf` drop-in), `canvas-browser.service`, `pab-dashboard-agent.service` (+`workdir.conf`), `assignment-caldav.service`. Splitting ops files into `pab-ops` does NOT require moving the live bot; units keep pointing at `~/personal-assistant-bot`.
- **`sudo` NOT passwordless**; heredocs hang Sanel's terminal → hand sudo as single-line commands or stage-then-`install`. `!`-prefixed/handed commands can silently no-op → verify live state after.

---

## Phase 0 — Prerequisites & safety net (no destructive ops)

### Task 0.1: Install missing tooling (no sudo)
**Objective:** Get `git-filter-repo` (cleanest for extracting a subtree into a fresh repo with pruned history).
```bash
pip install --user git-filter-repo
python -m git_filter_repo --version   # or: git filter-repo --version
```
Expected: version prints. If PATH lacks `~/.local/bin`, invoke via `python -m git_filter_repo`.
> Fallback if install blocked: use `git subtree split` (already available) — noted per-task below.

### Task 0.2: Set git identity (commits will be attributed to this)
```bash
git config --global user.name "Sanel Lathiya"
git config --global user.email "<sanel's github email>"   # CONFIRM with Sanel
```
Expected: `git config --global user.name` echoes it. (Open question: which email.)

### Task 0.3: Full backup before any split
```bash
cd ~ && tar --exclude='personal-assistant-bot/venv' \
  -czf pab-presplit-$(date +%Y%m%d).tar.gz personal-assistant-bot/
ls -lh pab-presplit-*.tar.gz
```
Expected: archive exists (includes `.git`, so this is the true rollback point).

### Task 0.4: Baseline test + service oracle
```bash
cd ~/personal-assistant-bot && source venv/bin/activate
pytest -q 2>&1 | tee /tmp/pab_baseline.txt | tail -20
systemctl is-active bot.service
git ls-files | wc -l         # expect 251
```
Expected: record pass count; note pre-existing failures (not ours to fix).

---

## Phase 1 — Extract `pab-study-content` (the true clean split, do FIRST)

This is the highest-value, lowest-risk repo to break out: it is pure data with zero import coupling, and it is what bloats the code repo. **DECISION: history-preserving, private.** Use `git filter-repo` on an **isolated clone** (never the live repo — shared worktree).

### Task 1.1: Extract content paths with full history on an isolated clone
```bash
# Clone the monorepo to /tmp so filter-repo can NEVER touch the live .git / worktree
git clone ~/personal-assistant-bot /tmp/pab-clone-content
cd /tmp/pab-clone-content
git checkout fix/2026-07-bug-audit
# Keep ONLY the study/content paths, preserving their history
git filter-repo --path study_guides/ --path knowledge_base/ \
  --path offline_archive/ --path mega_index.md \
  --path-glob '*.docx' --force
# filter-repo strips the origin remote by design — good, we re-point below
git log --oneline | wc -l    # history retained for these paths
du -sh .git                  # smaller than 495M but retains provenance
```
Expected: repo now contains only content paths + their commit history.
> **Secrets audit before adding a remote:** `git log --all --name-only --pretty=format: | sort -u | grep -iE 'env|cred|token|secret|state\.json'` → must be empty. filter-repo with only content paths should exclude all of these, but verify.

### Task 1.2: Create the private GitHub repo and push (no gh → curl)
```bash
set -a; . ~/.hermes/.env; set +a          # sources GITHUB_TOKEN (never echoed)
curl -s -X POST -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user/repos \
  -d '{"name":"pab-study-content","private":true,"description":"Generated study guides & knowledge base"}' | grep -E '"full_name"|"message"'
cd /tmp/pab-clone-content
git remote add origin https://github.com/SanelL112/pab-study-content.git
git push -u origin fix/2026-07-bug-audit:main
```
Expected: HTTP 201 from create; push succeeds. Verify: `curl -s -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/repos/SanelL112/pab-study-content | grep full_name`.
> **Verification (do not trust push self-report):** re-clone to /tmp and diff file count against source before declaring success.

### Task 1.3: Untrack the content from the monorepo (kept on disk, gitignored)
```bash
cd ~/personal-assistant-bot
# extend .gitignore (via write_file/patch tool)
git rm -r --cached --quiet study_guides knowledge_base offline_archive
git rm --cached --quiet '*.docx' mega_index.md 2>/dev/null
git commit -m "chore: move study content to pab-study-content repo; untrack here"
git ls-files | wc -l   # large drop
ls study_guides/ | head  # PROVE still on disk
```
Expected: ~100+ files leave the index; disk intact.

---

## Phase 2 — Extract `pab-ops` (infra repo, low coupling)

### Task 2.1: Decide the config-coupled scripts
4 ops scripts import `config` (`canvas_browser_daemon`, `canvas_new_course_watch`, `generate_daily_digest`, `telegram_notify`). Options:
- **2a (recommended):** keep those 4 in `pab-core` (they're bot-adjacent), move only the truly standalone infra to `pab-ops`.
- **2b:** move all, and add a minimal `config.py` shim / env-var reader to `pab-ops`.

### Task 2.2 (history-preserving): Extract via git subtree on an ISOLATED clone
> Never run on `~/personal-assistant-bot` (shared worktree). Clone first.
```bash
git clone ~/personal-assistant-bot /tmp/pab-clone-ops
cd /tmp/pab-clone-ops
git checkout fix/2026-07-bug-audit
# subtree split preserves history for these paths
git subtree split -P scripts -b split-scripts      # (repeat/merge surface/ as needed)
```
For multiple paths, `git filter-repo --path scripts/ --path surface/ --path bot.service` on the isolated clone is cleaner:
```bash
git clone ~/personal-assistant-bot /tmp/pab-clone-ops2 && cd /tmp/pab-clone-ops2
git filter-repo --path scripts/ --path surface/ --path bot.service --path pull_models.sh --path install-hooks.sh
```
Expected: repo now contains only ops paths with their history.

### Task 2.3: Create + push `pab-ops` (PUBLIC)
```bash
set -a; . ~/.hermes/.env; set +a
curl -s -X POST -H "Authorization: token $GITHUB_TOKEN" https://api.github.com/user/repos \
  -d '{"name":"pab-ops","private":false,"description":"Infra: systemd units, health checks, RPC cluster tooling"}' | grep -E '"full_name"|"message"'
cd /tmp/pab-clone-ops2
git remote add origin https://github.com/SanelL112/pab-ops.git
git push -u origin main   # or the split branch
```
Expected: 201 + push OK. Verify via re-clone.
> **Secrets audit before push (PUBLIC repo — critical):** `git log --all --name-only --pretty=format: | sort -u | grep -iE 'env|cred|token|secret|state\.json|\.key'` → MUST be empty. The ops paths (`scripts/`, `surface/`, units) should carry no secrets, but a public repo makes any leak irreversible — verify hard.

### Task 2.4: Remove ops files from the monorepo
```bash
cd ~/personal-assistant-bot
git rm -r --cached scripts surface   # or git rm if fully relocating
git rm --cached pull_models.sh install-hooks.sh 2>/dev/null
git commit -m "chore: move infra/ops to pab-ops repo"
```
> **DO NOT delete `bot.service` from disk** — the live unit references the file path only for docs; the actual unit lives in `/etc/systemd/system/`. Confirm `systemctl cat bot.service` still resolves after.

### Task 2.5: Verify live services untouched
```bash
systemctl is-active bot.service canvas-browser.service pab-dashboard-agent.service assignment-caldav.service
```
Expected: all `active`. (We moved *repo copies* of unit files, not the installed units.)

---

## Phase 3 — `pab-core` becomes the clean code repo (the live bot)

After Phases 1–2, `~/personal-assistant-bot` is now ~code-only. This IS `pab-core`. **DECISION: rename in place (3a), make PUBLIC, then worktree-safe gc.**

- **3a: rename the existing GitHub repo** `Antigravity-Based-Assistant-Bot` → `pab-core`, set public, keep the local repo/worktree/services exactly where they are. No history rewrite, no worktree risk.
  ```bash
  set -a; . ~/.hermes/.env; set +a
  curl -s -X PATCH -H "Authorization: token $GITHUB_TOKEN" \
    https://api.github.com/repos/SanelL112/Antigravity-Based-Assistant-Bot \
    -d '{"name":"pab-core","private":false}' | grep -E '"full_name"|"private"|"message"'
  cd ~/personal-assistant-bot
  git remote set-url origin https://github.com/SanelL112/pab-core.git
  git remote -v
  ```
  > GitHub auto-redirects the old URL, but update the remote explicitly. Confirm the `-release/` worktree's remote too: `git -C ~/personal-assistant-bot-release remote -v` (shares `.git`, so it's already updated — verify).
  > **PUBLIC repo secrets gate:** `.env`, `credentials.json`, `token.json`, `state.json` are gitignored (verified) — but before flipping public, run `git ls-files | grep -iE 'env|cred|token|secret|\.key|state\.json'` → must be empty. Also scan history: `git log --all --name-only --pretty=format: | sort -u | grep -iE 'credentials\.json|token\.json|\.env$'`. If any secret was EVER committed, do NOT make public until scrubbed.

### Task 3.1: Declutter dead code inside pab-core (safe, no history rewrite)
```bash
cd ~/personal-assistant-bot
git rm --quiet audit_script.py clean_emojis.py comprehensive_test.py \
  fix_bot_commands.py fix_utils_pii.py nightly_processor.py.bak run_builder.py.bak
mkdir -p docs/archive && git mv AGENT_BRIEFING.md AI_CONTEXT.md audit_report.md \
  BUG_REPORTS_COMBINED.md CONSOLIDATED_BUG_AUDIT_AND_REMEDIATION.md \
  REPAIR_CHANGES_SUMMARY.md SECURITY_AUDIT_FINDINGS.md SECURITY_AUDIT_REPORT.md \
  SPRINT_PLAN.md BUG_AUDIT_VERIFICATION_REPORT.md docs/archive/ 2>/dev/null
pytest -q 2>&1 | tail -5   # match baseline
git commit -m "chore: remove dead scripts, archive stale reports"
```

### Task 3.2: Worktree-safe `.git` shrink (reclaim ~415 MB of dangling objects)
**Objective:** `.git` is 495 MB but only ~80 MB is reachable — the rest is unreachable loose objects (2662 loose, in-pack:16; never gc'd). `git gc` prunes those WITHOUT rewriting reachable history, so it is safe for the shared `-release/` worktree.
```bash
cd ~/personal-assistant-bot
git reflog expire --expire=now --all      # drop reflog refs to dangling commits
git gc --prune=now                        # pack + prune unreachable objects
git count-objects -vH | grep -E 'count|size-pack'
du -sh .git                               # expect ~50-80M, down from 495M
```
Expected: `.git` drops to tens of MB; `pytest -q` still matches baseline; `systemctl is-active bot.service` = active (gc doesn't touch the working tree).
> This does NOT strip the big `.docx`/`.npz` from *reachable* history (that would need filter-repo on an isolated clone + force-push + worktree re-setup — deferred; not worth the worktree risk for the remaining ~80→~20 MB).

### Task 3.3 (optional): break the import cycle so a future 6-repo split is possible
Only if Sanel wants finer separation later. Move `bot/storage.py`, `bot/state.update_state`, `bot/ui.render_assistant_text`, `bot/dashboard_state.record_route` into a `pab/core/` module so `utils`/`llm_router`/`scrapers` no longer import `bot.*`. This is a code refactor with the pytest gate — defer unless requested.

---

## Files Likely to Change / Move

- **New repos:** `pab-study-content` (data), `pab-ops` (infra), `pab-core` (renamed from existing).
- **Untracked from monorepo (kept on disk):** study_guides/, knowledge_base/, offline_archive/, *.docx, mega_index.md, embedding_data/, backups/.
- **Moved to pab-ops:** scripts/, surface/, unit files, pull_models.sh, install-hooks.sh.
- **Deleted:** fix_*.py, *.bak, audit/clean/comprehensive one-offs; stale .md → docs/archive/.
- **Modified in pab-core:** `.gitignore`, `origin` remote URL.
- **Untouched (critical):** `/etc/systemd/system/*` installed units, `~/personal-assistant-bot-release` worktree, `state.json`/`.env`.

## Tests / Validation

- `pytest -q` in pab-core equals `/tmp/pab_baseline.txt` pass count after each phase.
- `systemctl is-active` for all 4 units = active after Phases 2 & 3.
- **Push verification (mandatory, self-reports are not proof):** for each new repo, re-clone to `/tmp` and `diff -r` file lists / `git log --oneline | wc -l` against the source. Confirm repo exists via `curl .../repos/SanelL112/<name>`.
- `du -sh .git` in each new repo (content/ops small; pab-core drops 495M→~50-80M after Task 3.2 gc).
- Files-on-disk proof (`ls <dir>`) after every `git rm --cached`.

## Risks, Tradeoffs (decisions locked — see LOCKED DECISIONS at top)

**Risks**
1. **Import cycles** (`utils`/`llm_router`/`scrapers` ↔ `bot.*`) make a per-layer 6-repo split impossible without a refactor. The chosen 3-repo split (core+ops+content) avoids this.
2. **Shared `.git` worktree** — every history-rewriting step (filter-repo for content/ops extraction) MUST run on an isolated `/tmp` clone. The pab-core `git gc` (Task 3.2) is reachability-preserving and safe. NEVER run filter-repo on `~/personal-assistant-bot/.git` — it corrupts `-release/`.
3. **`gh` not installed** → all GitHub API ops via `curl` + `GITHUB_TOKEN` from `~/.hermes/.env`. Token scope must include `repo` (verify: create call returns 201 / `full_name`).
4. **git identity** set to `SanelL12` / `sanel.lathiya@gmail.com` (Task 0.2). Repo OWNER in URLs stays `SanelL112`.
5. **Secrets hygiene (2 repos are PUBLIC):** `pab-core` and `pab-ops` are public → a leaked secret is irreversible. Mandatory pre-push audit of BOTH working tree (`git ls-files | grep -iE ...`) AND full history (`git log --all --name-only ...`) for `.env`/`credentials.json`/`token.json`/`state.json`/`.key`. `pab-study-content` is private but still audit it. Block any public flip if a secret was ever committed.
6. **Handed sudo / `!` commands may silently no-op** — none needed in this plan (no unit edits; services only read-verified), but verify `systemctl is-active` live after Phases 2 & 3 anyway.

**Tradeoffs**
- 3 repos (core/ops/content) is the natural, coupling-respecting boundary. A 6-repo per-layer split (Task 3.3) requires breaking cycles + publishing an internal package — deferred.
- pab-core keeps full reachable history (no blob-strip rewrite); Task 3.2 gc reclaims the ~415 MB of dangling cruft safely, which gets 90% of the shrink benefit at 0% of the worktree risk.

---

## Execution order (once `GITHUB_TOKEN` is in `~/.hermes/.env`)

Phase 0 (install/identity/backup/baseline) → Phase 1 (pab-study-content, private, history) → Phase 2 (pab-ops, public, history) → Phase 3 (rename → pab-core, public, dead-code cleanup, gc). Verify services + re-clone-diff each repo before declaring done.
