#!/bin/bash
# Build the browser Docker image on the cloud VM (ARM64-compatible).
# Run from ~/web-agent after the code has been rsync'd.
set -euo pipefail
cd ~/web-agent

echo "=== Building fara-browser (base) ==="
docker build -t fara-browser ./browser/

echo "=== Building fara-browser-pw (with socat/CDP) ==="
docker build -t fara-browser-pw ./browser_pw/

echo "=== Creating fara-net network (idempotent) ==="
docker network create fara-net 2>/dev/null || true

echo "=== Starting fara-browser-pw container ==="
docker rm -f fara-browser-pw 2>/dev/null || true
docker run -d \
  --name fara-browser-pw \
  --network fara-net \
  -p 127.0.0.1:9333:9333 \
  -p 127.0.0.1:5901:5900 \
  -v ~/web-agent/data_pw:/data \
  -v ~/web-agent/browser-profile-pw:/home/browser/chromium-profile \
  fara-browser-pw

echo "Waiting for browser to start..."
sleep 4
docker logs --tail 5 fara-browser-pw

echo ""
echo "DONE. Browser container running. Next: bash ~/web-agent/cloud/start-agent.sh"
