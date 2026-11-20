#!/bin/bash
# Setup locale una tantum: venv Python, rete e immagini Docker, container del
# browser (e del modello, se i GGUF sono in models/). Idempotente: si puo'
# rilanciare. I percorsi vengono dalla posizione di questo script, quindi la
# cartella del progetto si puo' spostare o rinominare senza toccare niente.
#
# Per il 9B (o un altro GGUF): FARA_LOCAL_MODEL / FARA_LOCAL_MMPROJ.
# Con un modello via API il container del modello non serve: salta questo
# script per la parte GPU e imposta FARA_URL / FARA_MODEL / FARA_API_KEY.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== venv Python =="
[ -x venv_pw/bin/python ] || python3 -m venv venv_pw
venv_pw/bin/pip install -q -r requirements.txt

echo "== rete e immagini Docker =="
docker network create fara-net 2>/dev/null || true
docker build -t fara-browser    docker/browser/
docker build -t fara-browser-pw docker/browser-pw/

echo "== container del browser (fara-browser-pw) =="
mkdir -p var/logs/pw var/profiles/pw
docker rm -f fara-browser-pw 2>/dev/null || true
docker run -d --name fara-browser-pw --restart unless-stopped \
  --network fara-net --shm-size 64m \
  --cap-drop ALL --security-opt no-new-privileges:true --pids-limit 1024 \
  -p 127.0.0.1:9333:9333 -p 127.0.0.1:5901:5900 \
  -v "$ROOT/fixtures/pw:/data" \
  -v "$ROOT/var/profiles/pw:/home/browser/chromium-profile" \
  fara-browser-pw

MODEL="${FARA_LOCAL_MODEL:-Fara1.5-27B-Q4_K_M.gguf}"
MMPROJ="${FARA_LOCAL_MMPROJ:-Fara1.5-27B.mmproj-q8_0.gguf}"
if [ -f "models/$MODEL" ]; then
    echo "== container del modello (fara-test) =="
    docker rm -f fara-test 2>/dev/null || true
    docker run -d --name fara-test --restart unless-stopped \
      --gpus all --network fara-net \
      --security-opt no-new-privileges:true \
      -p 127.0.0.1:8090:8080 \
      -v "$ROOT/models:/models:ro" \
      ghcr.io/ggml-org/llama.cpp:server-cuda \
      -m "/models/$MODEL" --mmproj "/models/$MMPROJ" \
      --host 0.0.0.0 --port 8080 \
      --reasoning-format none -ngl 99 -c 32768 \
      || echo "   modello non avviato (GPU o NVIDIA Container Toolkit mancanti?)."
else
    echo "== modello locale saltato: models/$MODEL non trovato."
    echo "   Metti i GGUF in models/ e rilancia, oppure usa un modello via API."
fi

echo
echo "Fatto. Avvia con ./scripts/start_gui_pw.sh"
