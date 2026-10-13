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

## Indice

- [Requisiti e hardware](#requisiti-e-hardware) · [Software da installare](#software-da-installare)
- [Modelli](#modelli)
- [Setup (una tantum)](#setup-una-tantum) — [Python](#1-ambiente-python) · [Docker](#2-immagini-e-rete-docker) · [modello](#3-container-del-modello-llamacpp) · [browser](#4-container-del-browser) · [identità](#5-identità-opzionale) · [**Telegram**](#6-telegram-opzionale)
- [Avvio](#avvio) e [spegnimento](#verifica-e-spegnimento)
- [Come si usa la GUI](#come-si-usa-la-gui) · [Guardrail](#guardrail)
- [Modalità di velocità](#modalità-di-velocità)
- [Dashboard delle performance](#dashboard-delle-performance)
- [**Controllo da Telegram**](#controllo-da-telegram)
- [API HTTP](#api-http-tutto-su-loopback) · [riga di comando](#uso-da-riga-di-comando-senza-gui)
- [Esecuzione su VM cloud](#esecuzione-su-vm-cloud)
- [Variabili d'ambiente](#variabili-dambiente) · [Log](#log) · [Test e benchmark](#test-e-benchmark) · [Struttura della repo](#struttura-della-repo)
- [NEXT_STEPS.md](NEXT_STEPS.md) — cosa manca e cosa va sistemato

## Requisiti e hardware

Il modello si può servire in due modi, e la differenza di hardware è tutta qui:

| | **A — modello in locale** | **B — modello via API** |
|---|---|---|
| Cosa serve | GPU NVIDIA + container llama.cpp | solo `FARA_URL` + `FARA_API_KEY` |
| Da scaricare | 10–18 GB di GGUF | niente |
| Costo | corrente | a token |
| Privacy | gli screenshot non lasciano il PC | gli screenshot vanno al provider |
| Dove gira | PC/workstation con GPU | anche una VM da 2 vCPU, [vedi](#esecuzione-su-vm-cloud) |

| Componente | Minimo | Consigliato | Note |
|---|---|---|---|
| GPU (solo opzione A) | 12 GB VRAM → 9B Q8 | 24 GB VRAM → 27B Q4 | con `-c 32768` il 27B Q4 sta in 24 GB (17,5 GB di pesi + 0,6 GB di mmproj + KV cache) |
| RAM | 8 GB (16 con opzione A) | 32 GB | il browser in container gira su ~1 GB |
| Disco | 5 GB + i modelli | 40 GB | 27B = 17,5 + 0,6 GB · 9B = 9,5 GB · i log tengono fino a 200 run di screenshot |
| CPU | 2 core | 8 core | Chromium, Xvfb e xdotool stanno nel container |
| Sistema | Linux con Docker | — | niente Windows/macOS: serve `docker exec` + display virtuale |

Con l'opzione B bastano **2 vCPU / 4 GB / 20 GB di disco** (è il taglio del free
tier ARM di Oracle Cloud, su cui sono tarati gli script in `scripts/cloud/`).

### Prestazioni misurate

Su RTX 5090 Laptop (24 GB VRAM), 24 core, 64 GB di RAM, con
`FARA_ENABLE_THINKING=0` (i dati completi sono in `bench/bench_models/`):

| | 27B Q4_K_M | 9B Q8_0 |
|---|---|---|
| Generazione | 35 tok/s | 73 tok/s |
| Lettura di uno screenshot (1281 token immagine) | 1,5 s | 0,6 s |
| Decisione di un click (mediana) | 2,9 s | 1,4 s |
| Click centrati | 24/24 (8/8 bersagli piccoli) | 23/24 (7/8) |
| Compilazione batch di 15 campi | 13,2 s | 5,4 s |
| Survey tipo Qualtrics portata a termine | 2/2 | 0/2 |

Il 9B è il doppio più veloce ma manca i bersagli piccoli e non chiude le survey
complesse: va bene per provare, per lavorare serve il 27B (o un modello via API).

### Software da installare

Sull'host servono solo tre cose:

- **Docker** (e il [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
  solo per l'opzione A).
- **Python 3.12** più un venv con `playwright` e `pillow` (passo 1 del setup).
- Un browser qualsiasi per aprire la GUI.

Tutto il resto — Chromium, Xvfb, xdotool, scrot, x11vnc, socat — è dentro
l'immagine Docker del browser: **non va installato sull'host**. Playwright si
collega al Chromium del container via CDP, quindi non serve `playwright install`.
Il bot Telegram usa solo la libreria standard di Python: nessuna dipendenza in
più.

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
venv_pw/bin/pip install -r requirements.txt
```

Playwright si collega al Chromium già in esecuzione nel container via CDP,
quindi non serve `playwright install`.

### 2. Immagini e rete Docker

```bash
docker network create fara-net

# immagine base del browser (Chromium + Xvfb + xdotool + x11vnc)
docker build -t fara-browser docker/browser/

# stessa immagine + socat per esporre CDP all'host
docker build -t fara-browser-pw docker/browser-pw/
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
mkdir -p var/logs/pw var/profiles/pw

docker run -d --name fara-browser-pw --restart unless-stopped \
  --network fara-net --shm-size 64m \
  -p 127.0.0.1:9333:9333 -p 127.0.0.1:5901:5900 \
  -v "$PWD/fixtures/pw:/data" \
  -v "$PWD/var/profiles/pw:/home/browser/chromium-profile" \
  fara-browser-pw
```

- `fixtures/pw/` è montata come `/data`: contiene le pagine di test
  (`file:///data/test.html`, ecc.) e gli screenshot temporanei.
- `var/profiles/pw/` è il profilo persistente: cookie e login restano tra
  un riavvio e l'altro. Contiene dati personali, non condividerlo.
- Porta 5901: VNC del display virtuale (senza password, solo loopback), utile
  per guardare/intervenire a mano con un client VNC.

### 5. Identità (opzionale)

I campi tipo "Prolific ID" o "email" vengono **copiati, mai inventati**. Se non
li metti nel contesto del compito, li legge da `agent/identity.json`:

```bash
cp agent/identity.json.example agent/identity.json
# poi compila "prolific_id" e "email"
```

Il file è nel `.gitignore`.

### 6. Telegram (opzionale)

Serve per comandare l'agente e ricevere notifiche dal telefono.

```bash
cp agent/telegram_config.py.example agent/telegram_config.py
```

Poi riempi i due valori: il **token** lo dà [@BotFather](https://t.me/BotFather)
con `/newbot`, il **chat id** si legge aprendo
`https://api.telegram.org/bot<TOKEN>/getUpdates` dopo aver scritto un messaggio
al bot (`result[0].message.chat.id`). Istruzioni anche dentro il file.

Se il file non c'è, bot e notifiche vengono saltati in silenzio e il resto
funziona identico. Il bot risponde **solo** ai messaggi che arrivano da quell'id.

> Il token è un segreto, e da solo basta per pilotare l'agente:
> `telegram_config.py` è nel `.gitignore` (come `identity.json` e
> `scripts/cloud/fara.env`), nella repo c'è solo il `.example`.
> Vedi [Controllo da Telegram](#controllo-da-telegram).

## Avvio

```bash
./scripts/start_gui_pw.sh     # avvia i container + la GUI e apre il browser su http://127.0.0.1:8766
./scripts/restart.sh          # riavvia solo il gui_server (dopo aver modificato il codice Python)
```

`scripts/start_gui_pw.sh` fa `docker start fara-test fara-browser-pw`, avvia
`gui_server.py` su `:8766` solo se non risponde già, e apre la GUI nel browser.
`scripts/restart.sh` in più ferma la run in corso e uccide il processo vecchio prima di
rilanciare. Il log del server finisce sempre in `var/logs/pw/gui_server.out`.

Oppure a mano:

```bash
docker start fara-test fara-browser-pw
cd agent && ../venv_pw/bin/python gui_server.py --port 8766
```

Con un modello via API il container `fara-test` non serve: basta esportare
`FARA_URL`, `FARA_MODEL` e `FARA_API_KEY` prima di avviare il server.

> Gli script ricavano i percorsi da dove si trovano, e il codice dalla
> posizione del file: la cartella del progetto si può spostare o rinominare
> senza modificare niente. I container, però, hanno i bind mount scritti nella
> loro configurazione: se sposti la cartella vanno ricreati con i nuovi `-v`
> (vedi il passo 4).

Avviando il server partono da soli anche due thread di servizio: il **bot
Telegram** (se `agent/telegram_config.py` esiste — manda `/help` al bot
per verificare che risponda) e l'aggiornamento degli studi recenti. Non c'è
niente da lanciare a parte.

### Verifica e spegnimento

```bash
curl -s localhost:8766/agent/status        # {"running": false, ...} = server su
tail -f var/logs/pw/gui_server.out             # errori di avvio
curl -s localhost:9333/json/version        # il CDP del container risponde?
curl -s localhost:8090/health              # il modello locale risponde?
```

```bash
curl -X POST localhost:8766/agent/stop     # ferma la run
pkill -f "gui_server.py --port 8766"       # ferma server, bot Telegram e stream
docker stop fara-browser-pw fara-test      # ferma browser e modello
```

> Se `/agent/status` mostra `"url": ""` mentre il ciclo risulta in esecuzione,
> la scheda del browser è bloccata: lo schermo continua a mostrare l'ultimo
> frame (lo screenshot passa da `scrot`, non da Playwright) ma ogni lettura del
> DOM resta appesa. Dal ciclo successivo l'agente se ne accorge e sostituisce la
> scheda da solo; a mano si fa così, senza riavviare il container e senza
> perdere il login:
>
> ```bash
> curl -X PUT "localhost:9333/json/new?https://app.prolific.com/studies"
> curl "localhost:9333/json/close/<id della vecchia scheda>"   # da /json/list
> ./scripts/restart.sh                                                 # riconnette il bridge
> ```

## Come si usa la GUI

Il pannello sta su `http://127.0.0.1:8766` (solo loopback).

**Sinistra**: lo schermo live del browser (con marcatore sul punto che l'agente
sta per cliccare). Quando l'agente è fermo puoi usare il **controllo manuale**:
barra indirizzi ("Vai"), campo "Digita" e tasti Invio/Tab/Canc/Esc. Serve ad
esempio per fare il **primo login** su un sito: il browser è in kiosk e non ha
barra degli indirizzi, quindi si naviga da qui. Il login resta nel profilo.

**Destra, tab "Impostazioni"**:

| Campo | Significato |
|---|---|
| Preset salvati | carica/salva/elimina combinazioni di impostazioni (salvate in `agent/presets.json`) |
| Contesto / persona | chi sei per questo compito (età, lavoro, competenze, dati usabili). Vale per tutti i cicli |
| Compito | cosa deve fare l'agente, in linguaggio naturale. Obbligatorio |
| Ripeti in loop + pausa | se attivo, finito un ciclo aspetta N secondi e ricomincia (utile per controllare periodicamente se compaiono nuovi studi). Un ciclo che va in errore non ferma più la run: viene segnalato e riprovato dopo 30 s, 1, 2 e poi 5 minuti, ripartendo dalla pagina di partenza |
| Compilazione batch | quando la pagina è un form con molte domande, estrae il DOM, fa decidere tutte le risposte al modello in una chiamata e le applica con input reale (verificando il risultato) invece di cliccare campo per campo |
| Salta studi con audio o video | se la pagina dello studio chiede cuffie, video, webcam o microfono, lo studio viene abbandonato dal codice e messo nella memoria degli scartati, invece di affidarsi alle istruzioni del compito |
| Vai all'URL all'avvio + URL | pagina da aprire prima di cominciare |
| Wander (secondi) | se resta troppo a lungo su pagine del sito che non sono l'elenco studi, lo riporta a `/studies`. `0` = disattivato |
| Passi massimi | tetto di step per ciclo |

Premi **▶ Avvia**. Il tab **Attività** mostra in tempo reale cosa pensa e fa
l'agente (fase, token generati, azione, risultato) e le statistiche (ciclo,
passi, batch, errori, tempo). Il bottone **📊** in alto apre la
[dashboard delle performance](#dashboard-delle-performance).

Nella barra in alto ci sono anche:

- **Studio `3:41 / 12:00`**: il [timer del tempo minimo](#guardrail). È giallo
  finché non si può inviare, verde quando il minimo è stato raggiunto; passandoci
  sopra dice quanto manca. Compare solo durante uno studio.
- **🐢 Normale / ⚡ Veloce / 🚀 Super**: la
  [modalità di velocità](#modalità-di-velocità), cambiabile a run in corso. Se il
  bordo è giallo è stata alzata da sola dal timer, e il tooltip dice a quale
  modalità tornerà a studio finito.

Finché una run è in corso il portatile non va in sospensione nemmeno a coperchio
chiuso: il server tiene un inhibitor `systemd-inhibit` (lid/sleep/idle) che vive
esattamente quanto la run, senza cambiare nessuna impostazione di sistema.

Durante la run:

- **Domande dell'agente**: se chiama `ask_user_question` compare una scheda con
  un campo di risposta nel tab Attività.
- **Istruzione extra**: la casella in basso invia un messaggio all'agente, che
  lo riceve dal passo successivo (vale per il ciclo corrente; in loop ogni
  ciclo riparte da zero, vedi `scripts/tools/inject_each_cycle.py`).
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
- **Memoria degli studi scartati**: ogni ciclo riparte senza memoria, quindi uno
  studio che è lì ma non si può fare (non idoneo, posti esauriti, già svolto,
  screened out) verrebbe riaperto ogni volta. Quando la pagina dello studio lo
  dice a chiare lettere, lo studio finisce in `agent/study_cache.json` e
  dal ciclo dopo l'agente si vede l'elenco dei titoli da non aprire; se lo riapre
  comunque, al primo passo viene rimbalzato all'elenco senza spendere altri
  passi. Il nome dello studio viene letto sulla pagina Prolific, non dentro la
  survey esterna (dove il titolo sarebbe quello del questionario). Le voci
  scadono dopo 14 giorni (`FARA_STUDY_CACHE_DAYS`). Si leggono con
  `curl localhost:8766/study_cache` e si azzerano con
  `curl -X POST localhost:8766/study_cache/clear`.
- **Audio/video (opzionale)**: con l'opzione attiva, un `<video>`/`<audio>` o un
  player incorporato fa abbandonare lo studio su qualunque pagina; le frasi tipo
  "you will need headphones" o "watch the video" vengono invece lette **solo**
  sulla pagina Prolific dello studio, dove sono una descrizione — dentro un
  questionario la stessa frase può essere una domanda, e abbandonare una survey
  a metà per un falso positivo costa molto più di un click sprecato.
- Le istruzioni nei preset (es. saltare studi con audio/video) sono solo testo
  del compito: modificale a piacere.

## Modalità di velocità

Tre modalità che barattano qualità per velocità. Si cambiano dai tre bottoni
nella barra della GUI, da Telegram (`/normal`, `/hurry`, `/superfast`) o con
`POST /agent/speed`, anche a run in corso:

| Modalità | Screenshot inviato al modello | `max_tokens` | Immagini in contesto |
|---|---|---|---|
| `normal` (default) | PNG a risoluzione piena | 1024 | max 10, si taglia a 5 |
| `hurry` | PNG ridotto al 75% | 512 | max 10, si taglia a 5 |
| `super_fast` | JPEG q60 ridotto al 50% | 384 | max 5, si taglia a 2 |

Lo screenshot viene ridimensionato **una volta sola**, direttamente dalla
cattura a piena risoluzione: ridurlo e poi lasciare che `smart_resize` lo
riportasse in parte su costava più byte dell'originale (411 KB contro 376 KB su
uno screenshot vero) perché la sfocatura introdotta è proprio quello che il PNG
comprime peggio.

Se uno studio sta sforando il tempo minimo stimato la velocità **sale da sola**:
`hurry` appena si supera il minimo, `super_fast` dopo 2 minuti di sforamento.
L'escalation non scende mai di livello e, finito lo studio (o tornando
all'elenco), si ripristina la modalità che avevi scelto tu — nella GUI il bordo
giallo sul bottone indica proprio questo.

## Dashboard delle performance

`http://127.0.0.1:8766/dashboard` (o il bottone **📊** nella GUI) legge tutti i
transcript in `var/logs/pw/run_*/` e mostra: cicli totali, completati (`CODE` /
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
| `/status` | screenshot live + stato, uptime, ciclo/passi/form/errori, fase, URL, timer dello studio, modalità velocità, quanti studi sono in memoria come scartati, studi recenti e guadagni delle ultime 24 h (solo studi approvati) |
| `/inject <testo>` | manda un'istruzione extra alla run in corso: l'agente la riceve al passo successivo (come la casella in basso nella GUI) |
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
  "batch_fill": true, "wander_seconds": 90, "skip_av": false }'

curl localhost:8766/agent/status                      # stato + eventi
curl -X POST localhost:8766/agent/inject -H 'Content-Type: application/json' -d '{"text":"..."}'
curl -X POST localhost:8766/agent/answer -H 'Content-Type: application/json' -d '{"text":"..."}'
curl -X POST localhost:8766/agent/stop

curl -X POST localhost:8766/agent/speed -H 'Content-Type: application/json' \
     -d '{"mode":"hurry"}'                            # normal | hurry | super_fast

curl localhost:8766/study_cache                       # studi scartati in memoria
curl -X POST localhost:8766/study_cache/clear         # svuota la memoria
curl -X POST localhost:8766/study_cache/forget -H 'Content-Type: application/json' \
     -d '{"key":"<id studio>"}'                       # ne riabilita uno
```

`/agent/status` riporta anche `speed` (modalità in corso, quella scelta
dall'utente e se è stata alzata da sola) e `study_timer`.

Altri endpoint: `/screen.png`, `/stream.mjpg` (video), `/agent/events` (SSE),
`/dashboard` (HTML), `/presets`, `/click`, `/type`, `/key`, `/navigate`.

## Uso da riga di comando (senza GUI)

```bash
cd agent
../venv_pw/bin/python controller.py "Clicca il bottone BLU." --url file:///data/test.html --max-rounds 10
```

## Esecuzione su VM cloud

Con un modello servito da un'API remota la GPU non serve più, quindi l'agente può
stare sempre acceso su una VM piccola (gli script sono tarati su una Ubuntu 22.04
ARM64, es. Oracle Cloud free tier). In `scripts/cloud/`:

| Script | Dove si lancia | Cosa fa |
|---|---|---|
| `setup-vm.sh` | sulla VM, una volta | Docker, Python venv (da `requirements.txt`), cartelle (`fixtures/pw`, `var/logs/pw`, `var/profiles/pw`) |
| `sync-to-cloud.sh user@ip [dir]` | dal PC locale | rsync del codice **e del profilo browser già loggato** (così su Prolific non devi rifare il login). Non copia `fara.env`, i log e i modelli |
| `build-docker.sh` | sulla VM | costruisce `fara-browser` / `fara-browser-pw` e avvia il container del browser |
| `start-agent.sh` | sulla VM | carica `scripts/cloud/fara.env` e avvia `gui_server.py` su `:8766`, con log in `var/logs/pw/gui_server.out` |
| `fara-agent.service` | sulla VM | unit systemd (`Restart=always`) per tenerlo su anche dopo reboot o crash; i log vanno nel journal (`journalctl -u fara-agent -f`) |

Gli script non hanno path assoluti dentro: lavorano tutti su `FARA_HOME`
(default `~/web-agent`), quindi funzionano con qualunque nome utente. La
configurazione si copia dal `.example` e sta in un solo file:

```bash
cp scripts/cloud/fara.env.example scripts/cloud/fara.env
```

Dentro ci sono l'API remota (`FARA_URL`, `FARA_MODEL`, `FARA_API_KEY`),
`FARA_TOKEN_BUDGET` più generoso (le API cloud hanno finestre di contesto molto
più grandi del 27B locale) e `FARA_HOME` con i path della VM. systemd legge quel
file alla lettera, senza espandere `~` o `$HOME`: se l'utente della VM non è
`ubuntu`, il `sed` scritto in testa ai due file sistema tutto.

> `fara.env` contiene una chiave API ed è nel `.gitignore` (nella repo c'è solo
> il `.example`). La GUI resta in ascolto solo su loopback, quindi per usarla dal
> tuo PC apri un tunnel:
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
| `FARA_DATA_DIR` | `~/web-agent/fixtures/pw` | cartella montata come `/data` |
| `FARA_LOG_ROOT` | `~/web-agent/var/logs/pw` | dove salva le run (`run_*/transcript.jsonl` + screenshot) |
| `FARA_MAX_RUNS` | `200` | run di cui tenere gli screenshot; oltre, il transcript viene compresso e gli screenshot cancellati |
| `FARA_PRESETS` | `agent/presets.json` | file dei preset |
| `FARA_GUARDED_HOSTS` | `prolific.com,prolific.co` | siti con le sezioni vietate |
| `FARA_STUDIES_URL` | `https://app.prolific.com/studies` | dove tornare quando uno studio viene abbandonato |
| `FARA_STUDY_CACHE` | `agent/study_cache.json` | memoria degli studi scartati |
| `FARA_STUDY_CACHE_DAYS` | `14` | dopo quanti giorni uno studio scartato può essere riaperto |
| `FARA_TEST_INSTANCE` | *(vuota)* | `1` su un secondo server di prova: niente bot Telegram (due poller sullo stesso token si rubano i messaggi) e niente lettura delle submission (aprirebbe una scheda nel browser vero) |
| `FARA_ENABLE_THINKING` | `0` | `1` = lascia il thinking del template. Sul 9B peggiora molto i click, sul 27B è indifferente |
| `FARA_STREAM_FPS` / `FARA_STREAM_QUALITY` | `5` / `60` | stream MJPEG della GUI |

## Log

Ogni run scrive in `var/logs/pw/run_AAAAMMGG_HHMMSS/`: `transcript.jsonl` (cosa ha
pensato/fatto a ogni passo) e `screenshot_NN.png`. Nel transcript, accanto alle
righe del dialogo col modello, ci sono righe `{"type": "diag"}` con lo stato
"meccanico" di ogni passo (URL, sito guardato sì/no, azione, risultato, stato del
timer, durata letta dalla card): servono per capire a posteriori perché il timer
è partito o no. La dashboard aggrega tutti i transcript presenti; `var/logs/pw/`
tiene le ultime 200 run e cancella le più vecchie.

Passate le ultime `FARA_MAX_RUNS` run (200) gli screenshot vengono cancellati —
sono loro a riempire il disco, uno per passo — mentre il transcript viene
compresso in `transcript.jsonl.gz` e **tenuto per sempre**: pesa qualche decina
di KB ed è l'unica traccia di cosa ha fatto l'agente. Dashboard e conteggio
degli studi completati leggono entrambe le forme, quindi lo storico delle
statistiche non si perde.

Altri file in `var/logs/pw/`: `gui_server.out` (log del server quando è avviato dagli
script cloud o da systemd) e `submissions_api_debug.txt` (ultimo esito della
lettura delle submission di Prolific).

## Test e benchmark

I test usano le pagine di `fixtures/pw/` / `fixtures/test/` (form semplici, complessi,
iframe, nuova scheda, click "trusted", survey tipo Qualtrics, ecc.).

> **Non lanciare test mentre una run vera è in corso**: toccano lo stesso
> browser. Controlla prima `curl localhost:8766/agent/status`. Per i test
> end-to-end usa lo stack isolato: un secondo `gui_server` sulla porta **8767**
> con il container `fara-browser-test` (profilo `var/profiles/test/`, dati
> `fixtures/test/`, log `var/logs/test/`), puntandolo con `FARA_CONTAINER`,
> `FARA_CDP_URL`, `FARA_DATA_DIR`, `FARA_LOG_ROOT` e **`FARA_TEST_INSTANCE=1`**
> (senza, il secondo server fa polling Telegram sullo stesso token della live e
> apre schede nel browser per leggere le submission).

Il container di prova esiste già (fermo) con i suoi mount; per i test che
toccano il DOM basta:

```bash
docker start fara-browser-test          # CDP :9334, VNC :5902
cd agent
FARA_TEST_INSTANCE=1 FARA_CONTAINER=fara-browser-test \
FARA_CDP_URL=http://127.0.0.1:9334 \
FARA_DATA_DIR="$PWD/../fixtures/test" FARA_LOG_ROOT="$PWD/../var/logs/test" \
../venv_pw/bin/python tests_resilience.py --port 8767
```

| Script | Cosa verifica |
|---|---|
| `agent/tests_pw.py` | layer Playwright: ogni risultato è riletto dal DOM finale |
| `agent/tests_resilience.py` | guardrail e velocità. Metà **offline** (modalità di velocità, veto del timer su `terminate`, rilevamento studi scartati e audio/video, memoria degli studi, rotazione dei log, backoff dei retry): `--offline` la esegue senza browser né rete, in qualunque momento. L'altra metà tocca il DOM e **si rifiuta di partire se una run è in corso** |
| `agent/tests_stop.py [base_url]` | quanto ci mette Stop a fermare la run nei 3 casi lenti |
| `agent/gui_e2e.py` | GUI end-to-end. Ha `http://127.0.0.1:8766` scritto nel codice e avvia una run vera: cambia la porta in 8767 prima di lanciarlo |
| `agent/gui_live_e2e.py [base_url]` | feed SSE, stream MJPEG, marcatori (rifiuta la porta 8766) |
| `agent/model_bench.py <label>` | benchmark di un modello su :8090 (velocità, grounding, giudizio, batch-fill) → `bench/bench_models/<label>.json` |
| `agent/decision_bench.py <label>` | qualità delle risposte del batch-fill |
| `bench/bench_agents.py`, `bench/bench_run_one.py` | run complete su scenari fissi (multipage, newtab, iframe, trusted) con verifica indipendente |

I risultati dei benchmark già fatti (27B Q4 vs 9B Q8) sono in `bench/bench_models/` e
nei file `bench/*.out`.

## Struttura della repo

```
agent/              il codice dell'agente: agent_core (loop e guardrail), pw_bridge
                    (ponte Playwright), form_fill_pw (compilazione batch),
                    gui_server + gui_index.html (pannello), study_cache,
                    controller.py (uso da riga di comando), test
docker/
  browser/          immagine base: Chromium kiosk + Xvfb + xdotool + x11vnc + cdp.py
  browser-pw/       la stessa, più socat per esporre il CDP all'host
fixtures/
  pw/               pagine HTML di prova montate come /data nel browser live
  test/             le stesse per lo stack isolato di test
scripts/
  start_gui_pw.sh   avvia container + GUI e apre il pannello
  restart.sh        riavvia solo il gui_server (dopo modifiche al codice)
  cloud/            deploy su VM: setup-vm, sync-to-cloud, build-docker,
                    start-agent, fara-agent.service, fara.env.example
  tools/            utility (inject_each_cycle.py)
bench/              benchmark e risultati già raccolti (bench_models/, *.out)
models/             modelli GGUF — ignorata da git, vedi "Modelli"
var/                tutto ciò che è runtime, ignorato da git:
  logs/pw/          una cartella per run (transcript + screenshot) e gui_server.out
  logs/test/        lo stesso per lo stack di test
  profiles/pw/      profilo Chromium del browser live: cookie e login, non condividerlo
  profiles/test/    profilo dello stack di test
venv_pw/            ambiente Python (requirements.txt)
```

I file che contengono dati personali o segreti stanno fuori da git
(`agent/identity.json`, `agent/presets.json`, `agent/study_cache.json`,
`agent/telegram_config.py`, `scripts/cloud/fara.env`): nella repo ci sono solo
i rispettivi `.example` da copiare.

`agent/survey_loop.py` e `agent/form_fill.py` sono la versione
precedente (basata su `cdp.py`); la pipeline attuale usa `form_fill_pw.py`, che
riusa la logica di decisione di `form_fill.py`.
