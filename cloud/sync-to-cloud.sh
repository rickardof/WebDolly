#!/bin/bash
# Sync local code to the cloud VM via rsync (run this from your local PC).
# Usage: bash cloud/sync-to-cloud.sh ubuntu@YOUR_VM_IP
set -euo pipefail

REMOTE="${1:?Usage: $0 user@ip}"

echo "Syncing code to $REMOTE..."
rsync -avz --progress \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.git' \
  --exclude='logs_pw/' \
  --exclude='data_pw/' \
  --exclude='browser-profile-pw/' \
  --exclude='venv_pw/' \
  /home/riccardo/web-agent/ \
  "$REMOTE:~/web-agent/"

echo ""
echo "Syncing Prolific browser profile (logged-in session)..."
rsync -avz --progress \
  /home/riccardo/web-agent/browser-profile-pw/ \
  "$REMOTE:~/web-agent/browser-profile-pw/"

echo ""
echo "Done. On the VM, run: bash ~/web-agent/cloud/start-agent.sh"
