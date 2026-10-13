# web-agent

Un agente che usa un browser reale al posto tuo, guidato da **Fara 1.5**
(modello vision-language che guarda gli screenshot e decide dove cliccare/cosa
scrivere). Il caso d'uso principale è la compilazione di survey/studi (es.
Prolific) con un profilo/persona che definisci tu, ma il compito è libero: lo
scrivi nella GUI.

Di base tutto gira in locale (il modello può anche stare su un'API remota,
vedi sotto):

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
- **Controller**: gira sull'host (Python). Espone la GUI web su `127.0.0.1:8766`,
  una dashboard delle performance su `/dashboard` e un bot Telegram per
  comandare tutto dal telefono.
- **Modello remoto (alternativa alla GPU)**: `FARA_URL` + `FARA_API_KEY` possono
  puntare a qualunque API OpenAI-compatibile con visione (es. Gemini). In quel
  caso il container `fara-test` e la GPU non servono e l'agente gira anche su una
  VM piccola: vedi [Esecuzione su VM cloud](#esecuzione-su-vm-cloud).

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

### 6. Telegram (opzionale)

Serve per comandare l'agente e ricevere notifiche dal telefono. Crea un bot con
[@BotFather](https://t.me/BotFather), prendi il token e l'id della tua chat, e
scrivili in `controller_pw/telegram_config.py`:

```python
TELEGRAM_TOKEN   = "123456:ABC-DEF..."
TELEGRAM_CHAT_ID = "12345678"
```

Se il file non c'è, bot e notifiche vengono saltati in silenzio e il resto
funziona identico. Il bot risponde **solo** ai messaggi che arrivano da quell'id.

> Il token è un segreto: tieni `telegram_config.py` fuori da git (come
> `identity.json`). Vedi [Controllo da Telegram](#controllo-da-telegram).

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
passi, batch, errori, tempo). Il bottone **📊** in alto apre la
[dashboard delle performance](#dashboard-delle-performance).

Finché una run è in corso il portatile non va in sospensione nemmeno a coperchio
chiuso: il server tiene un inhibitor `systemd-inhibit` (lid/sleep/idle) che vive
esattamente quanto la run, senza cambiare nessuna impostazione di sistema.

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
- **Timer dello studio**: parte quando si passa da Prolific a un sito esterno,
  qualunque sia il modo (click, Invio in un modale, redirect JS), con il minimo
  letto dalla card dello studio che ha cliccato (e, se lì non lo trova, dal testo
  della pagina). Finché mancano più di 30 s al minimo l'azione `terminate` viene
  **rifiutata** e all'agente viene detto di usare `wait`. Tornando a `/studies` il
  timer si ri-arma per lo studio successivo.
- La **compilazione batch** è disattivata sulle pagine dei siti guardati: si usa
  solo sui questionari esterni, non sull'interfaccia di Prolific.
- **Click verificati**: dopo un click su una checkbox ne rilegge lo stato dal DOM
  e, se non è cambiato, la ri-clicca via JS. Se il click finisce su un elemento
  disabilitato (`disabled`, `aria-disabled`, classe `disabled`) l'agente viene
  avvisato che probabilmente è rimasto un campo obbligatorio da compilare.
- A studio completato chiude le schede extra rimaste aperte, così il ciclo
  successivo riparte da una sola scheda.
- Le istruzioni nei preset (es. saltare studi con audio/video) sono solo testo
  del compito: modificale a piacere.

## Modalità di velocità

Tre modalità che barattano qualità per velocità (si cambiano da Telegram e
valgono per tutta la run):

| Modalità | Screenshot inviato al modello | `max_tokens` | Immagini in contesto |
|---|---|---|---|
| `normal` (default) | PNG a risoluzione piena | 1024 | max 10, si taglia a 5 |
| `hurry` | PNG ridotto al 75% | 512 | max 10, si taglia a 5 |
| `super_fast` | JPEG q60 ridotto al 50% | 384 | max 5, si taglia a 2 |

Se uno studio sta sforando il tempo minimo stimato la velocità **sale da sola**:
`hurry` appena si supera il minimo, `super_fast` dopo 2 minuti di sforamento.
L'escalation non scende mai di livello e, finito lo studio (o tornando
all'elenco), si ripristina la modalità che avevi scelto tu.

## Dashboard delle performance

`http://127.0.0.1:8766/dashboard` (o il bottone **📊** nella GUI) legge tutti i
transcript in `logs_pw/run_*/` e mostra: cicli totali, completati (`CODE` /
`NOCODE`), cicli senza esito (`NONE`), incompleti, durata media per studio
completato, cicli/ora sul tempo effettivo della sessione, un istogramma
dell'attività per ora e la tabella degli studi completati con codice, durata e
numero di passi. Si aggiorna da sola ogni 60 s e, mentre l'agente gira, ha un
bottone **⏹ Ferma**.

## Controllo da Telegram

Con `telegram_config.py` configurato (vedi [setup](#6-telegram-opzionale)) il
server fa polling dei comandi del bot. Tutti i comandi hanno anche la versione
italiana (`avvia`, `ferma`, `riavvia`, `stato`, `veloce`, `velocissimo`,
`aiuto`), con o senza `/`:

| Comando | Cosa fa |
|---|---|
| `/start` | avvia l'agente con il **primo preset salvato**, senza navigare: parte da dov'è il browser |
| `/stop` | ferma subito la run (come il tasto ■) |
| `/restart` | ferma, riporta il browser su `/studies`, riavvia |
| `/status` | screenshot live + stato, uptime, ciclo/passi/form/errori, fase, URL, timer dello studio, modalità velocità, studi recenti e guadagni delle ultime 24 h (solo studi approvati) |
| `/normal`, `/hurry`, `/superfast` | cambiano [modalità di velocità](#modalità-di-velocità) |
| `/help` | la lista dei comandi |

Notifiche automatiche: 📝 studio iniziato (con la durata stimata e l'URL), ✅
studio completato (con il codice o `NOCODE`), ❌ errore della run (tipo di
eccezione e coda del traceback).

Gli "studi recenti" e i guadagni arrivano dalla pagina `submissions` di
Prolific, letta in una scheda temporanea aperta **solo nella pausa fra due
cicli** (mai durante uno step), con cache di 60 s e aggiornamento a fine ciclo o
ogni 5 minuti. Se la lettura fallisce, resta in cache il risultato precedente.

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
`/dashboard` (HTML), `/presets`, `/click`, `/type`, `/key`, `/navigate`.

## Uso da riga di comando (senza GUI)

```bash
cd controller_pw
../venv_pw/bin/python controller.py "Clicca il bottone BLU." --url file:///data/test.html --max-rounds 10
```

## Esecuzione su VM cloud

Con un modello servito da un'API remota la GPU non serve più, quindi l'agente può
stare sempre acceso su una VM piccola (gli script sono tarati su una Ubuntu 22.04
ARM64, es. Oracle Cloud free tier). In `cloud/`:

| Script | Dove si lancia | Cosa fa |
|---|---|---|
| `setup-vm.sh` | sulla VM, una volta | Docker, Python venv, cartelle (`data_pw`, `logs_pw`, `browser-profile-pw`) |
| `sync-to-cloud.sh user@ip` | dal PC locale | rsync del codice **e del profilo browser già loggato** (così su Prolific non devi rifare il login) |
| `build-docker.sh` | sulla VM | costruisce `fara-browser` / `fara-browser-pw` e avvia il container del browser |
| `start-agent.sh` | sulla VM | carica `cloud/fara.env` e avvia `gui_server.py` su `:8766`, con log in `logs_pw/gui_server.out` |
| `fara-agent.service` | sulla VM | unit systemd (`Restart=always`) per tenerlo su anche dopo reboot o crash |

La configurazione sta in `cloud/fara.env`: `FARA_URL` / `FARA_MODEL` /
`FARA_API_KEY` dell'API remota, `FARA_TOKEN_BUDGET` più generoso (le API cloud
hanno finestre di contesto molto più grandi del 27B locale) e i path della VM.

> `fara.env` contiene una chiave API: non committarlo. La GUI resta in ascolto
> solo su loopback, quindi per usarla dal tuo PC apri un tunnel:
> `ssh -L 8766:localhost:8766 utente@IP_DELLA_VM`.

Quando `FARA_API_KEY` è impostata il controller si adatta da solo: manda
l'header `Authorization: Bearer ...`, parla in HTTPS, non invia
`chat_template_kwargs` (è un'estensione di llama-server) e, non avendo più
l'endpoint `/tokenize`, stima i token con un fallback a ~4 caratteri per token.

## Variabili d'ambiente

| Variabile | Default | Cosa fa |
|---|---|---|
| `FARA_URL` | `http://127.0.0.1:8090/v1/chat/completions` | endpoint del modello (chat completions, OpenAI-compatibile) |
| `FARA_MODEL` | `Fara1.5-27B-Q4_K_M.gguf` | nome del modello passato nella richiesta |
| `FARA_API_KEY` | *(vuota)* | se impostata: `Authorization: Bearer`, HTTPS e modalità "API cloud" (vedi sopra) |
| `FARA_TOKEN_BUDGET` | `28000` | tetto di token del contesto prima del taglio di immagini/messaggi |
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
pensato/fatto a ogni passo) e `screenshot_NN.png`. Nel transcript, accanto alle
righe del dialogo col modello, ci sono righe `{"type": "diag"}` con lo stato
"meccanico" di ogni passo (URL, sito guardato sì/no, azione, risultato, stato del
timer, durata letta dalla card): servono per capire a posteriori perché il timer
è partito o no. La dashboard aggrega tutti i transcript presenti; `logs_pw/`
tiene le ultime 200 run e cancella le più vecchie.

Altri file in `logs_pw/`: `gui_server.out` (log del server quando è avviato dagli
script cloud o da systemd) e `submissions_api_debug.txt` (ultimo esito della
lettura delle submission di Prolific).

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
                    + telegram_config.py (token del bot, da tenere privato)
cloud/              deploy su VM: setup-vm.sh, sync-to-cloud.sh, build-docker.sh,
                    start-agent.sh, fara-agent.service, fara.env
data_pw/, data_test/  pagine HTML di test montate come /data
tools/              utility (inject_each_cycle.py)
bench_models/       risultati dei benchmark
models/             modelli GGUF (ignorata da git)
start_gui_pw.sh, restart.sh   avvio / riavvio
```

`controller_pw/survey_loop.py` e `controller_pw/form_fill.py` sono la versione
precedente (basata su `cdp.py`); la pipeline attuale usa `form_fill_pw.py`, che
riusa la logica di decisione di `form_fill.py`.
