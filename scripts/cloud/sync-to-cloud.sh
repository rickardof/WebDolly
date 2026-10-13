#!/bin/bash
# Sync local code to the cloud VM via rsync (run this from your local PC).
# Usage: bash scripts/cloud/sync-to-cloud.sh user@vm-ip [remote-dir]
#
# The source is this checkout, found from the script's own location, so the
# script works wherever the repo lives.
set -euo pipefail

REMOTE="${1:?Usage: $0 user@ip [remote-dir]}"
REMOTE_DIR="${2:-web-agent}"
LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "Syncing $LOCAL_DIR -> $REMOTE:~/$REMOTE_DIR ..."
rsync -avz --progress \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='var/' \
  --exclude='fixtures/*/_*.png' \
  --exclude='venv_pw/' \
  --exclude='models/' \
  --exclude='scripts/cloud/fara.env' \
  "$LOCAL_DIR/" \
  "$REMOTE:~/$REMOTE_DIR/"

echo ""
echo "Syncing the browser profile (logged-in Prolific session)..."
echo "NOTE: contains cookies and login data — only sync to a VM you control."
rsync -avz --progress \
  "$LOCAL_DIR/var/profiles/pw/" \
  "$REMOTE:~/$REMOTE_DIR/var/profiles/pw/"

echo ""
echo "Done. On the VM: cp scripts/cloud/fara.env.example scripts/cloud/fara.env (fill it in),"
echo "then bash ~/$REMOTE_DIR/scripts/cloud/start-agent.sh"
