#!/bin/bash
# Run this script ONCE on a fresh Ubuntu 22.04 VM (tested on Oracle Cloud ARM).
# Installs Docker, a Python venv, and prepares the directory layout.
#
# Everything lives under $FARA_HOME (default: ~/web-agent), so this works
# whatever the VM user is called -- no path is hardcoded to a username.
set -euo pipefail

FARA_HOME="${FARA_HOME:-$HOME/web-agent}"

echo "=== 1. System update ==="
sudo apt-get update -q
sudo apt-get upgrade -y -q

echo "=== 2. Docker ==="
sudo apt-get install -y -q ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -q
sudo apt-get install -y -q docker-ce docker-ce-cli containerd.io
sudo usermod -aG docker "$USER"

echo "=== 3. Python deps ==="
sudo apt-get install -y -q python3-pip python3-venv python3-dev

echo "=== 4. Directory layout in $FARA_HOME ==="
mkdir -p "$FARA_HOME"/{var/logs/pw,var/profiles/pw}

echo "=== 5. Python venv ==="
python3 -m venv "$FARA_HOME/venv_pw"
if [ -f "$FARA_HOME/requirements.txt" ]; then
  "$FARA_HOME/venv_pw/bin/pip" install --quiet -r "$FARA_HOME/requirements.txt"
else
  # requirements.txt arrives with sync-to-cloud.sh; install the minimum now.
  "$FARA_HOME/venv_pw/bin/pip" install --quiet playwright pillow
fi

echo ""
echo "DONE. Log out and back in (or run: newgrp docker) to use Docker without sudo."
echo "Next: bash $FARA_HOME/scripts/cloud/build-docker.sh"
