#!/usr/bin/env bash
# deploy_and_verify.sh — standing "verify → commit → push → deploy" loop
# for the nabu-trader Fly.io bot.
#
# Usage:
#   ./scripts/deploy_and_verify.sh "feat: short description" [--skip-tests]
#
# Prereqs: run from git-bash/MSYS (the Hermes terminal). The Windows flyctl
# binary lives at /c/Users/it26/.fly/bin — this script adds it to PATH.
# Must be on branch fix/code-review-fixes (or pass BRANCH env).
set -euo pipefail

REPO="/c/Working Folder/Research/nabu-trader"
APP="nabu-trader"
BRANCH="${BRANCH:-fix/code-review-fixes}"
FLY="$HOME/.fly/bin"
export PATH="$PATH:$FLY"

cd "$REPO"

[[ $# -ge 1 ]] || { echo "usage: $0 \"commit message\""; exit 1; }
MSG="$1"

# 1. Verify — run the venv test suite first.
if [[ "${2:-}" != "--skip-tests" ]]; then
  echo "==> [1] pytest"
  source .venv/Scripts/activate
  python -m pytest -q
fi

# 2. Stage specific paths (NEVER -A — avoids .hermes/ + stray files).
echo "==> [2] git add src/ tests/ + commit"
git add src/ tests/ AGENTS.md CHANGELOG.md docs/ scripts/ 2>/dev/null || true
git commit -m "$MSG"

# 3. Push.
echo "==> [3] git push origin $BRANCH"
git push origin "$BRANCH"

# 4. Deploy (rolling update).
echo "==> [4] flyctl deploy"
flyctl deploy

# 5. Health check.
echo "==> [5] health + boot logs"
curl -s -o /dev/null -w "HTTP %{http_code}\n" "https://$APP.fly.dev/health"
flyctl logs --no-tail --app "$APP" | grep -E "Listening for new messages|sendMessage" | tail -5 || true

echo "==> done. Verify the latest deploy hash with: flyctl status --app $APP"
