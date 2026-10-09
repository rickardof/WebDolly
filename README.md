# web-agent

Un agente che usa un browser reale al posto tuo, guidato da **Fara 1.5**
(modello vision-language che guarda gli screenshot e decide dove cliccare/cosa
scrivere). Il caso d'uso principale è la compilazione di survey/studi (es.
Prolific) con un profilo/persona che definisci tu, ma il compito è libero: lo
scrivi nella GUI.

Tutto gira in locale:

```
┌────────────── host ──────────────┐   ┌──────── Docker ────────┐
│ gui_server.py  (porta 8766)      │   │ fara-test   (llama.cpp │
│  └─ agent_core.py  (loop agente) │──▶│   + GPU, porta 8090)   │  modello Fara 1.5
│  └─ form_fill_pw.py (batch-fill) │   ├────────────────────────┤
│  └─ pw_bridge.py ── Playwright ──┼──▶│ fara-browser-pw        │  Chromium kiosk su Xvfb
└──────────────────────────────────┘   │   CDP :9333, VNC :5901 │  (profilo persistente)
                                       └────────────────────────┘
```

- **Modello**: `llama-server` (llama.cpp, immagine CUDA) serve Fara via API
  OpenAI-compatibile su `127.0.0.1:8090`.
- **Browser**: Chromium in modalità kiosk dentro un container, su un display
  virtuale 1440x900. L'agente vede quello schermo e agisce con xdotool
  (mouse/tastiera) + Playwright via CDP (navigazione, lettura DOM, compilazione).
- **Controller**: gira sull'host (Python). Espone la GUI web su `127.0.0.1:8766`.

## Requisiti

- Linux con Docker e una GPU NVIDIA + [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
  (il 27B Q4 occupa ~17 GB + mmproj, più KV cache: serve una GPU grossa; il 9B Q8 è più leggero).
- Python 3.12 sull'host.
- Spazio disco: i modelli pesano ~27 GB in totale.

## Modelli

I modelli **non sono nella repo** (`models/` è nel `.gitignore`). Mettili in
`models/` (la cartella viene montata nel container come `/models`):

| File | Cosa è |
|---|---|
| `Fara1.5-27B-Q4_K_M.gguf` + `Fara1.5-27B.mmproj-q8_0.gguf` | modello 27B quantizzato + proiettore visione (quello usato di default) |
| `Fara1.5-9B-Q8_0.gguf` + `mmproj-Fara1.5-9B-bf16.gguf` | alternativa più piccola/veloce |

Ogni modello ha bisogno del **suo** file `mmproj`. Scaricali dalla sorgente da
cui hai preso Fara 1.5 in formato GGUF e copiali in `models/`.

## Setup (una tantum)

### 1. Ambiente Python

```bash
python3 -m venv venv_pw
venv_pw/bin/pip install playwright pillow websocket-client
```

Playwright si collega al Chromium già in esecuzione nel container via CDP,
quindi non serve `playwright install`.

### 2. Immagini e rete Docker

```bash
docker network create fara-net

# immagine base del browser (Chromium + Xvfb + xdotool + x11vnc)
docker build -t fara-browser browser/

# stessa immagine + socat per esporre CDP all'host
docker build -t fara-browser-pw browser_pw/
```

### 3. Container del modello (llama.cpp)

```bash
docker run -d --name fara-test --restart unless-stopped \
  --gpus all --network fara-net \
  -p 127.0.0.1:8090:8080 \
  -v "$PWD/models:/models" \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -m /models/Fara1.5-27B-Q4_K_M.gguf \
  --mmproj /models/Fara1.5-27B.mmproj-q8_0.gguf \
  --host 0.0.0.0 --port 8080 \
  --reasoning-format none -ngl 99 -c 32768
```

Per usare il 9B cambia `-m` e `--mmproj` con i file 9B. Con il 9B conviene
lasciare il thinking spento (default): vedi `FARA_ENABLE_THINKING` sotto.

### 4. Container del browser

```bash
mkdir -p data_pw browser-profile-pw logs_pw

docker run -d --name fara-browser-pw --restart unless-stopped \
  --network fara-net --shm-size 64m \
  -p 127.0.0.1:9333:9333 -p 127.0.0.1:5901:5900 \
  -v "$PWD/data_pw:/data" \
  -v "$PWD/browser-profile-pw:/home/browser/chromium-profile" \
  fara-browser-pw
```

- `data_pw/` è montata come `/data`: contiene le pagine di test
  (`file:///data/test.html`, ecc.) e gli screenshot temporanei.
- `browser-profile-pw/` è il profilo persistente: cookie e login restano tra
  un riavvio e l'altro. Contiene dati personali, non condividerlo.
- Porta 5901: VNC del display virtuale (senza password, solo loopback), utile
  per guardare/intervenire a mano con un client VNC.

### 5. Identità (opzionale)

I campi tipo "Prolific ID" o "email" vengono **copiati, mai inventati**. Se non
li metti nel contesto del compito, li legge da `controller_pw/identity.json`:

```bash
cp controller_pw/identity.json.example controller_pw/identity.json
# poi compila "prolific_id" e "email"
```

Il file è nel `.gitignore`.

## Avvio

```bash
./start_gui_pw.sh     # avvia i container + la GUI e apre il browser su http://127.0.0.1:8766
./restart.sh          # riavvia solo il gui_server (dopo aver modificato il codice Python)
```

Oppure a mano:

```bash
docker start fara-test fara-browser-pw
cd controller_pw && ../venv_pw/bin/python gui_server.py --port 8766
```

> Gli script `.sh` hanno il percorso `/home/riccardo/web-agent` scritto dentro:
> se sposti la cartella, aggiornali.

## Come si usa la GUI (http://127.0.0.1:8766)

**Sinistra**: lo schermo live del browser (con marcatore sul punto che l'agente
sta per cliccare). Quando l'agente è fermo puoi usare il **controllo manuale**:
barra indirizzi ("Vai"), campo "Digita" e tasti Invio/Tab/Canc/Esc. Serve ad
esempio per fare il **primo login** su un sito: il browser è in kiosk e non ha
barra degli indirizzi, quindi si naviga da qui. Il login resta nel profilo.

**Destra, tab "Impostazioni"**:

| Campo | Significato |
|---|---|
| Preset salvati | carica/salva/elimina combinazioni di impostazioni (salvate in `controller_pw/presets.json`) |
| Contesto / persona | chi sei per questo compito (età, lavoro, competenze, dati usabili). Vale per tutti i cicli |
| Compito | cosa deve fare l'agente, in linguaggio naturale. Obbligatorio |
| Ripeti in loop + pausa | se attivo, finito un ciclo aspetta N secondi e ricomincia (utile per controllare periodicamente se compaiono nuovi studi) |
| Compilazione batch | quando la pagina è un form con molte domande, estrae il DOM, fa decidere tutte le risposte al modello in una chiamata e le applica con input reale (verificando il risultato) invece di cliccare campo per campo |
| Vai all'URL all'avvio + URL | pagina da aprire prima di cominciare |
| Wander (secondi) | se resta troppo a lungo su pagine del sito che non sono l'elenco studi, lo riporta a `/studies`. `0` = disattivato |
| Passi massimi | tetto di step per ciclo |

Premi **▶ Avvia**. Il tab **Attività** mostra in tempo reale cosa pensa e fa
l'agente (fase, token generati, azione, risultato) e le statistiche (ciclo,
passi, batch, errori, tempo).

Durante la run:

- **Domande dell'agente**: se chiama `ask_user_question` compare una scheda con
  un campo di risposta nel tab Attività.
- **Istruzione extra**: la casella in basso invia un messaggio all'agente, che
  lo riceve dal passo successivo (vale per il ciclo corrente; in loop ogni
  ciclo riparte da zero, vedi `tools/inject_each_cycle.py`).
- **■ Stop**: ferma subito, anche a metà di una richiesta al modello.

### Guardrail

- Sui siti in `FARA_GUARDED_HOSTS` (default `prolific.com,prolific.co`) l'agente
  non può entrare in `/about-you` né nelle pagine di logout: viene riportato
  all'elenco studi.
- Rispetta il tempo minimo di completamento dello studio: se prova a inviare
  troppo presto, gli viene detto di aspettare.
- Le istruzioni nei preset (es. saltare studi con audio/video) sono solo testo
  del compito: modificale a piacere.

## API HTTP (tutto su loopback)

La GUI è un client di queste API, quindi puoi pilotare l'agente anche da script:

```bash
# avvia
curl -X POST localhost:8766/start_agent -H 'Content-Type: application/json' -d '{
  "task": "Vai su ... e compila ...", "url": "https://esempio.it",
  "max_rounds": 60, "loop": false, "poll_interval": 60,
  "batch_fill": true, "wander_seconds": 90 }'

curl localhost:8766/agent/status                      # stato + eventi
curl -X POST localhost:8766/agent/inject -H 'Content-Type: application/json' -d '{"text":"..."}'
curl -X POST localhost:8766/agent/answer -H 'Content-Type: application/json' -d '{"text":"..."}'
curl -X POST localhost:8766/agent/stop
```

Altri endpoint: `/screen.png`, `/stream.mjpg` (video), `/agent/events` (SSE),
`/presets`, `/click`, `/type`, `/key`, `/navigate`.

## Uso da riga di comando (senza GUI)

```bash
cd controller_pw
../venv_pw/bin/python controller.py "Clicca il bottone BLU." --url file:///data/test.html --max-rounds 10
```

## Variabili d'ambiente

| Variabile | Default | Cosa fa |
|---|---|---|
| `FARA_URL` | `http://127.0.0.1:8090/v1/chat/completions` | endpoint del modello |
| `FARA_CONTAINER` | `fara-browser-pw` | container del browser |
| `FARA_CDP_URL` | `http://127.0.0.1:9333` | CDP di Chromium (per Playwright) |
| `FARA_DATA_DIR` | `~/web-agent/data_pw` | cartella montata come `/data` |
| `FARA_LOG_ROOT` | `~/web-agent/logs_pw` | dove salva le run (`run_*/transcript.jsonl` + screenshot, ne tiene 200) |
| `FARA_PRESETS` | `controller_pw/presets.json` | file dei preset |
| `FARA_GUARDED_HOSTS` | `prolific.com,prolific.co` | siti con le sezioni vietate |
| `FARA_ENABLE_THINKING` | `0` | `1` = lascia il thinking del template. Sul 9B peggiora molto i click, sul 27B è indifferente |
| `FARA_STREAM_FPS` / `FARA_STREAM_QUALITY` | `5` / `60` | stream MJPEG della GUI |

## Log

Ogni run scrive in `logs_pw/run_AAAAMMGG_HHMMSS/`: `transcript.jsonl` (cosa ha
pensato/fatto a ogni passo) e `screenshot_NN.png`.

## Test e benchmark

I test usano le pagine di `data_pw/` / `data_test/` (form semplici, complessi,
iframe, nuova scheda, click "trusted", survey tipo Qualtrics, ecc.).

> **Non lanciare test mentre una run vera è in corso**: toccano lo stesso
> browser. Controlla prima `curl localhost:8766/agent/status`. Per i test
> end-to-end usa lo stack isolato: un secondo `gui_server` sulla porta **8767**
> con il container `fara-browser-test` (profilo `browser-profile-test/`, dati
> `data_test/`, log `logs_test/`), puntandolo con `FARA_CONTAINER`,
> `FARA_CDP_URL`, `FARA_DATA_DIR`, `FARA_LOG_ROOT`.

| Script | Cosa verifica |
|---|---|
| `controller_pw/tests_pw.py` | layer Playwright: ogni risultato è riletto dal DOM finale |
| `controller_pw/tests_stop.py [base_url]` | quanto ci mette Stop a fermare la run nei 3 casi lenti |
| `controller_pw/gui_e2e.py` | GUI end-to-end. Ha `http://127.0.0.1:8766` scritto nel codice e avvia una run vera: cambia la porta in 8767 prima di lanciarlo |
| `controller_pw/gui_live_e2e.py [base_url]` | feed SSE, stream MJPEG, marcatori (rifiuta la porta 8766) |
| `controller_pw/model_bench.py <label>` | benchmark di un modello su :8090 (velocità, grounding, giudizio, batch-fill) → `bench_models/<label>.json` |
| `controller_pw/decision_bench.py <label>` | qualità delle risposte del batch-fill |
| `bench_agents.py`, `bench_run_one.py` | run complete su scenari fissi (multipage, newtab, iframe, trusted) con verifica indipendente |

I risultati dei benchmark già fatti (27B Q4 vs 9B Q8) sono in `bench_models/` e
nei file `bench_*.out`.

## Struttura della repo

```
browser/            Dockerfile + entrypoint + policy (autofill/password manager disattivati) + cdp.py
browser_pw/         Dockerfile che aggiunge socat per esporre CDP
controller_pw/      codice dell'agente (agent_core, form_fill*, pw_bridge, gui_server, gui_index.html, test)
data_pw/, data_test/  pagine HTML di test montate come /data
tools/              utility (inject_each_cycle.py)
bench_models/       risultati dei benchmark
models/             modelli GGUF (ignorata da git)
start_gui_pw.sh, restart.sh   avvio / riavvio
```

`controller_pw/survey_loop.py` e `controller_pw/form_fill.py` sono la versione
precedente (basata su `cdp.py`); la pipeline attuale usa `form_fill_pw.py`, che
riusa la logica di decisione di `form_fill.py`.
