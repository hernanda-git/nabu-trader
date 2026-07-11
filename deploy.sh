#!/usr/bin/env bash
# deploy.sh — thin wrapper to the canonical deploy helper.
#
# The real "verify → commit → push → deploy" loop now lives in
# scripts/deploy_and_verify.sh (runs from git-bash/MSYS with the Windows
# flyctl binary on PATH). This wrapper keeps the old `bash deploy.sh`
# invocation working.
#
# Usage: bash deploy.sh "commit message"
set -euo pipefail
cd "$(dirname "$0")"
exec bash scripts/deploy_and_verify.sh "${1:-deploy: $(date +%Y%m%d-%H%M%S)}"
