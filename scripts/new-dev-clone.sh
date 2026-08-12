#!/usr/bin/env bash
# new-dev-clone.sh — create an isolated, runnable dev/debug clone of pab-core
# WITHOUT touching the live bot, its state, its .git, or the shared worktree.
#
# The live working dir (~/personal-assistant-bot) IS the running bot. A plain
# `git clone` from GitHub is missing every gitignored runtime file (.env,
# credentials.json, token.json, state.json) so it won't run. This script clones
# fresh, copies the secrets/credentials, and — critically — points the clone's
# RUNTIME/STATE at its OWN isolated directory (via PAB_STATE_DIR etc.) so debugging
# in the clone can never corrupt the live bot's state.json.
#
# Usage:
#   ./new-dev-clone.sh                # -> ~/pab-dev  (default)
#   ./new-dev-clone.sh /path/to/dest  # custom location
#   LIVE=~/personal-assistant-bot ./new-dev-clone.sh ~/pab-dev
#
# After it finishes, work in the clone freely. Push from the clone as normal;
# the live bot is untouched until YOU deploy (git pull in the live dir + restart).

set -euo pipefail

LIVE="${LIVE:-$HOME/personal-assistant-bot}"
DEST="${1:-$HOME/pab-dev}"
REMOTE="${REMOTE:-https://github.com/SanelL112/pab-core.git}"
BRANCH="${BRANCH:-fix/2026-07-bug-audit}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ -d "$LIVE/.git" ] || die "live repo not found at $LIVE"
[ -e "$DEST" ] && die "destination $DEST already exists — remove it or pick another path"

# 1. Fresh clone from GitHub (its OWN .git — never the shared worktree object store)
say "Cloning $REMOTE ($BRANCH) -> $DEST"
git clone --branch "$BRANCH" "$REMOTE" "$DEST"

cd "$DEST"

# 2. Isolated runtime dir INSIDE the clone (state.json etc. live here, not shared)
RUNTIME="$DEST/.devruntime"
mkdir -p "$RUNTIME/logs"
say "Isolated runtime dir: $RUNTIME"

# 3. Copy secrets/credentials the clone needs to run (gitignored, so not in the clone)
say "Copying credentials from live (.env, token.json, credentials.json, mcp-tokens)"
for f in .env credentials.json token.json; do
  [ -f "$LIVE/$f" ] && cp -p "$LIVE/$f" "$DEST/$f" && echo "   copied $f"
done
[ -d "$LIVE/mcp-tokens" ] && cp -rp "$LIVE/mcp-tokens" "$DEST/mcp-tokens" && echo "   copied mcp-tokens/"

# 4. Seed state from live so debugging has realistic data — but into the ISOLATED dir,
#    so writes never hit the live state.json.
if [ -f "$LIVE/state.json" ]; then
  cp -p "$LIVE/state.json" "$RUNTIME/state.json"
  echo "   seeded isolated state.json (a COPY — live state is untouched)"
fi

# 5. Isolate runtime via an activation shim.
#    IMPORTANT: config.py reads PAB_* DIRECTORY vars with os.getenv() at import time
#    and does NOT push .env into os.environ (it only reads .env into a local dict for
#    feature settings). So PAB_STATE_DIR etc. MUST be REAL environment variables — not
#    .env lines. We write an env.sh the developer sources; it exports the overrides and
#    activates the venv in one step.
say "Writing isolation shim (env.sh) — PAB_* must be real env vars, not .env lines"
cat > "$DEST/env.sh" <<EOF
# source this to work in the isolated dev clone:  source env.sh
export PAB_CONFIG_DIR="$DEST"
export PAB_RUNTIME_DIR="$RUNTIME"
export PAB_STATE_DIR="$RUNTIME"
export PAB_LOG_DIR="$RUNTIME/logs"
export PAB_EXPORT_DIR="$RUNTIME/exports"
export PAB_ENV_FILE="$DEST/.env"
source "$DEST/venv/bin/activate"
echo "dev clone active — STATE isolated at $RUNTIME"
EOF

# 6. Virtualenv + deps (runtime + test deps so pytest works in the clone)
say "Creating venv and installing requirements (this can take a minute)"
python3 -m venv venv
# shellcheck disable=SC1091
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
# Test deps (now tracked in requirements-test.in): needed for the hermetic suite
[ -f requirements-test.in ] && pip install -q -r requirements-test.in >/dev/null 2>&1 || \
  pip install -q pytest pytest-asyncio pytest-mock >/dev/null 2>&1 || true

# 7. Verify: fresh clone is runnable AND state is isolated (with the shim env vars)
say "Verifying the clone imports + state isolation"
export PAB_CONFIG_DIR="$DEST" PAB_RUNTIME_DIR="$RUNTIME" PAB_STATE_DIR="$RUNTIME" \
       PAB_LOG_DIR="$RUNTIME/logs" PAB_EXPORT_DIR="$RUNTIME/exports"
python - <<PY
import importlib
for m in ("config", "main"):
    importlib.import_module(m)
import config
ok = str(config.STATE_DIR) == "$RUNTIME"
print("   config.BASE_DIR   ->", config.BASE_DIR)
print("   config.STATE_FILE ->", config.STATE_FILE)
print("   ISOLATION:", "OK (state points into .devruntime)" if ok else "FAILED — state would hit BASE_DIR")
import sys; sys.exit(0 if ok else 1)
PY

say "Running test suite in the clone"
python -m pytest -q 2>&1 | tail -3 || true

say "DONE. Isolated dev clone ready at: $DEST"
cat <<EOF

  - Code:     fresh from GitHub ($BRANCH), independent .git (worktree-safe)
  - Secrets:  copied from live ($LIVE)
  - State:    ISOLATED at $RUNTIME (live bot's state.json is untouched)

Work here freely:
  cd $DEST && source env.sh      # activates venv + isolates state via PAB_* env vars
  python main.py                 # runs against isolated state — safe
  # edit / debug / commit / push as normal — does NOT affect the live bot

To DEPLOY reviewed changes to the live bot (only when ready):
  cd $LIVE && git pull
  sudo systemctl restart bot.service     # hand this sudo line to the operator
  # then verify:  systemctl is-active bot.service
EOF
