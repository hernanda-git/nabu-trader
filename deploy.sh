#!/usr/bin/env bash
# ────────────────────────────────────────────────────────────
# deploy.sh — Commit, version, and deploy to Fly.io
# ────────────────────────────────────────────────────────────
# Usage: bash deploy.sh [commit-message]
#   Without args, uses "deploy: v<N>" as commit message.
# ────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")"

WIN_BASE="/mnt/c/Working Folder/Research/nabu-trader"

# 1. Sync WSL → Windows
echo "=== Syncing files WSL → Windows ==="
rsync -a --delete \
  --exclude '.git/' --exclude '__pycache__/' --exclude '*.pyc' \
  --exclude '.env' --exclude 'sessions/' --exclude 'data/' --exclude 'logs/' \
  ./ "$WIN_BASE/"

# 2. Detect next version from Fly.io
echo "=== Detecting next Fly.io release version ==="
CURRENT=$(powershell.exe -NoProfile -Command \
  "flyctl apps releases --app nabu-trader" 2>/dev/null \
  | grep -E '^\s+v[0-9]+' | head -1 | sed 's/^[[:space:]]*//' | cut -d' ' -f1)
CURRENT_NUM=$(echo "$CURRENT" | sed 's/v//')
NEXT_NUM=$((CURRENT_NUM + 1))
NEXT="v${NEXT_NUM}"
echo "Current: $CURRENT → Next: $NEXT"

# 3. Write version file
echo "=== Writing src/version.py ==="
cat > src/version.py << EOF
"""Package version — updated automatically during deploy."""

__version__ = "${NEXT}"
EOF

cp src/version.py "$WIN_BASE/src/version.py"

# 4. Commit
echo "=== Committing ==="
MSG="${1:-deploy: ${NEXT}}"
cd "$WIN_BASE"
git add -A
git commit -m "$MSG"

# 5. Deploy
echo "=== Deploying ${NEXT} to Fly.io ==="
powershell.exe -NoProfile -Command \
  "flyctl deploy --app nabu-trader --detach"

echo ""
echo "✅ ${NEXT} deployed!"
echo "Run: flyctl apps releases --app nabu-trader"
