#!/bin/bash
# Run this script ONCE on the fresh Oracle Cloud Ubuntu 22.04 ARM VM.
# Installs Docker, Python venv, and prepares the directory layout.
set -euo pipefail

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

echo "=== 4. Directory layout ==="
mkdir -p ~/web-agent/{data_pw,logs_pw,browser-profile-pw,cloud}

echo "=== 5. Python venv ==="
python3 -m venv ~/web-agent/venv_pw
~/web-agent/venv_pw/bin/pip install --quiet playwright pillow

# Install Chromium for Playwright (headless, host-side)
~/web-agent/venv_pw/bin/playwright install chromium --with-deps 2>/dev/null || true

echo ""
echo "DONE. Log out and back in (or run: newgrp docker) to use Docker without sudo."
echo "Next: run  bash ~/web-agent/cloud/build-docker.sh"
