#!/bin/bash
# Build the browser Docker image on the VM (ARM64-compatible) and start it.
# Run after the code has been rsync'd by sync-to-cloud.sh.
set -euo pipefail

FARA_HOME="${FARA_HOME:-$HOME/web-agent}"
cd "$FARA_HOME"

echo "=== Building fara-browser (base) ==="
docker build -t fara-browser ./docker/browser/

echo "=== Building fara-browser-pw (with socat/CDP) ==="
docker build -t fara-browser-pw ./docker/browser-pw/

echo "=== Creating fara-net network (idempotent) ==="
docker network create fara-net 2>/dev/null || true

echo "=== Starting fara-browser-pw container ==="
mkdir -p "$FARA_HOME/fixtures/pw" "$FARA_HOME/var/profiles/pw"
docker rm -f fara-browser-pw 2>/dev/null || true
docker run -d \
  --name fara-browser-pw \
  --restart unless-stopped \
  --network fara-net \
  --shm-size 64m \
  --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 1024 \
  --security-opt seccomp="$FARA_HOME/docker/seccomp-chromium.json" \
  ${FARA_NO_SANDBOX:+-e FARA_NO_SANDBOX=1} \
  -p 127.0.0.1:9333:9333 \
  -p 127.0.0.1:5901:5900 \
  -v "$FARA_HOME/fixtures/pw:/data" \
  -v "$FARA_HOME/var/profiles/pw:/home/browser/chromium-profile" \
  fara-browser-pw

echo "Waiting for browser to start..."
sleep 4
docker logs --tail 5 fara-browser-pw

echo ""
echo "DONE. Browser container running. Next: bash $FARA_HOME/scripts/cloud/start-agent.sh"
