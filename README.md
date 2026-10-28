# web-dolly

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

- **Modello** (`fara-test`): llama.cpp serve Fara via API OpenAI su `127.0.0.1:8090`.
  In alternativa `FARA_URL`+`FARA_API_KEY` puntano a un'API remota con visione (es.
  Gemini): niente GPU né container, gira anche su una VM piccola ([VM cloud](#esecuzione-su-vm-cloud)).
- **Browser** (`fara-browser-pw`): Chromium kiosk in un container (display virtuale
  1440x900); l'agente lo guida con xdotool e Playwright via CDP.
- **Controller** (host, Python): la GUI su `127.0.0.1:8766`, la
  [dashboard](#dashboard-delle-performance) su `/dashboard` e il bot Telegram.

## Avvio rapido

Container e `venv_pw` già pronti:

```bash
./scripts/start_gui_pw.sh     # avvia i container + la GUI, apre http://127.0.0.1:8766
```

Nella GUI scrivi **contesto** (chi sei) e **compito**, poi premi ▶ Avvia (o da
Telegram `/start`). Dopo una modifica al codice Python: `./scripts/restart.sh`.

**Prima volta?** Fai una volta il [Setup](#setup-una-tantum) (venv, immagini Docker,
container del modello e del browser), poi il comando qui sopra. Con un modello via
API la GPU non serve: vedi [Esecuzione su VM cloud](#esecuzione-su-vm-cloud).

Il resto del documento: [Requisiti](#requisiti-e-hardware) · [Setup](#setup-una-tantum) ·
[GUI](#come-si-usa-la-gui) · [Guardrail](#guardrail) · [Velocità](#modalità-di-velocità) ·
[Telegram](#controllo-da-telegram) · [API HTTP](#api-http-tutto-su-loopback) ·
[Variabili d'ambiente](#variabili-dambiente) · [Test](#test-e-benchmark) ·
[cosa manca](NEXT_STEPS.md).

## Requisiti e hardware

Il modello si può servire in due modi, e la differenza di hardware è tutta qui:

| | **A: modello in locale** | **B: modello via API** |
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
| Sistema | Linux con Docker | idem | niente Windows/macOS: serve `docker exec` + display virtuale |

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

Tutto il resto (Chromium, Xvfb, xdotool, scrot, x11vnc, socat) è dentro
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

Prerequisiti sull'host: **Docker** (più il [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/)
per il modello in locale) e **Python 3.12**. Per l'opzione A metti prima i GGUF in
`models/` (vedi [Modelli](#modelli)). Il resto (Chromium, Xvfb, xdotool, ...) sta
nell'immagine Docker, non si installa a mano.

```bash
./scripts/setup.sh
```

Crea il venv (`requirements.txt`), la rete `fara-net`, costruisce le immagini e
avvia i container `fara-browser-pw` e, se i GGUF sono in `models/`, `fara-test` col
modello. È idempotente; i comandi esatti stanno dentro lo script. Per il 9B imposta
`FARA_LOCAL_MODEL`/`FARA_LOCAL_MMPROJ` (col 9B tieni il thinking spento). Con un
modello via API il container del modello e la GPU non servono: imposta
`FARA_URL`/`FARA_MODEL`/`FARA_API_KEY` (vedi [VM cloud](#esecuzione-su-vm-cloud)).

Due file **opzionali**, da copiare dai `.example` (entrambi nel `.gitignore`):

```bash
cp agent/identity.json.example      agent/identity.json
cp agent/telegram_config.py.example agent/telegram_config.py
```

- `agent/identity.json`: "Prolific ID", email e simili, campi che l'agente
  **copia, mai inventa** (quando non li passi nel contesto del compito).
- `agent/telegram_config.py`: per comandare l'agente dal telefono. Token da
  [@BotFather](https://t.me/BotFather) e chat id (istruzioni nel file); senza, bot e
  notifiche sono saltati in silenzio, e il bot risponde solo a quel chat id. Il token
  è un segreto.

Il profilo del browser (`var/profiles/pw/`) è persistente: cookie e login restano tra
i riavvii; contiene dati personali, non condividerlo. VNC del display su
`127.0.0.1:5901` per guardare o intervenire a mano.

## Avvio

Lo start quotidiano è in [Avvio rapido](#avvio-rapido). `start_gui_pw.sh` fa
`docker start fara-test fara-browser-pw` e avvia `gui_server.py` su `:8766` solo se
non risponde già; `restart.sh` in più ferma la run e uccide il processo vecchio
(log in `var/logs/pw/gui_server.out`). A mano, o con un modello via API (allora
`fara-test` non serve, basta esportare `FARA_URL`/`FARA_MODEL`/`FARA_API_KEY`):

```bash
docker start fara-test fara-browser-pw
cd agent && ../venv_pw/bin/python gui_server.py --port 8766
```

Col server partono da soli il **bot Telegram** (se `telegram_config.py` esiste,
manda `/help` per provarlo) e l'aggiornamento degli studi recenti.

> Script e codice ricavano i percorsi da dove si trovano: la cartella si può
> spostare o rinominare senza toccare niente. I container no: hanno i bind mount
> nella loro configurazione, quindi se sposti la cartella vanno ricreati con i
> nuovi `-v` (passo 4).

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

> Se `/agent/status` dà `"url": ""` a ciclo in esecuzione, la scheda è bloccata:
> lo schermo mostra l'ultimo frame (via `scrot`) ma ogni lettura del DOM resta
> appesa. L'agente la sostituisce da solo dal ciclo dopo; a mano, senza riavviare
> il container né perdere il login:
>
> ```bash
> curl -X PUT "localhost:9333/json/new?https://app.prolific.com/studies"
> curl "localhost:9333/json/close/<id della vecchia scheda>"   # da /json/list
> ./scripts/restart.sh                                                 # riconnette il bridge
> ```

## Come si usa la GUI

Il pannello sta su `http://127.0.0.1:8766` (solo loopback).

**Sinistra**: lo schermo live del browser (con marcatore sul prossimo click). A
agente fermo c'è il **controllo manuale** (barra indirizzi, "Digita",
Invio/Tab/Canc/Esc): serve soprattutto per il **primo login** a un sito, dato che
il browser è in kiosk senza barra indirizzi. Il login resta nel profilo.

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

- Sui siti in `FARA_GUARDED_HOSTS` (default `prolific.com,prolific.co`) l'agente non
  entra in `/about-you` né nelle pagine di logout: viene riportato all'elenco studi.
- **Timer dello studio**: parte passando da Prolific a un sito esterno (click, Invio
  in un modale o redirect JS), col minimo letto dalla card dello studio (o dal testo
  della pagina). Finché mancano più di 30 s `terminate` viene **rifiutato** e
  all'agente si dice di usare `wait`; a `/studies` si ri-arma per lo studio dopo.
- **Batch fill** disattivato sui siti guardati: solo sui questionari esterni.
- **Click verificati**: dopo un click su una checkbox ne rilegge lo stato dal DOM e,
  se non è cambiato, la ri-clicca via JS. Un click su un elemento disabilitato
  (`disabled`/`aria-disabled`/classe `disabled`) avvisa che resta un campo obbligatorio.
- A studio completato chiude le schede extra, così il ciclo dopo riparte da una sola.
- **Memoria degli studi scartati**: uno studio listato ma non fattibile (non idoneo,
  posti esauriti, già svolto, screened out), quando la pagina lo dice chiaramente,
  finisce in `agent/study_cache.json`; dal ciclo dopo l'agente ne vede il titolo e, se
  lo riapre, viene rimbalzato all'elenco al primo passo. Il nome è letto sulla pagina
  Prolific (non dentro la survey). Scade dopo 14 giorni (`FARA_STUDY_CACHE_DAYS`); si
  legge con `curl localhost:8766/study_cache`, si azzera con `.../study_cache/clear`.
- **Audio/video (opzionale)**: un `<video>`/`<audio>` o un player fa abbandonare lo
  studio su qualunque pagina; le frasi tipo "you will need headphones" vengono invece
  lette **solo** sulla pagina Prolific (dentro un questionario potrebbero essere una
  domanda, e abbandonare per un falso positivo costa più di un click sprecato).
- **Codice di completamento**, tre casi: (1) lo studio rimanda a Prolific con
  `…/submissions/complete?cc=XXXX` → l'agente legge il codice **dall'URL** e chiude
  senza interpellare il modello; (2) il codice è **sulla pagina** (testo, campo,
  iframe, o `?code=XXXX` nell'URL) → torna su Prolific, clicca "I'm finished" e lo
  scrive; tutti i codici visti vengono annotati e confrontati con quello inserito, e
  se non torna (o chiude `NOCODE` con un codice presente) arriva un ⚠️ su Telegram
  (segnala soltanto, non corregge); (3) nessun codice → chiude con `NOCODE` (un URL
  senza `cc` non vuol dire "niente codice", solo che lì non c'è).
- **Falso completamento**: se il modello chiude con `terminate` a testo libero (non
  `CODE:`/`NOCODE`) mentre è ancora sul sito della survey e nessun codice è comparso,
  di solito si è fermato sulla pagina di ringraziamento saltando il bottone finale che
  riporta a Prolific. La chiusura viene rifiutata una volta sola, con l'invito a cercare
  quel controllo (Submit/Finish/Return to Prolific); se richiude, viene obbedito.
- **Attesa sull'elenco senza modello**: in loop la pagina viene ricaricata e letta dal
  DOM ogni ~45 s; con "Waiting for studies" il modello non viene chiamato, parte solo
  se c'è qualcosa, se la pagina non è riconoscibile, o ogni 15 min per sicurezza.
- **Velocità del modello**: ogni risposta registra i tempi di llama-server
  (`/agent/status` e transcript); sotto i 10 token/s per 6 risposte di fila (di solito
  ~30) arriva un avviso, di solito risolto con `docker restart fara-test`.
- Le istruzioni nei preset (es. saltare audio/video) sono solo testo del compito.

## Modalità di velocità

Tre modalità che barattano qualità per velocità. Si cambiano dai tre bottoni
nella barra della GUI, da Telegram (`/normal`, `/hurry`, `/superfast`) o con
`POST /agent/speed`, anche a run in corso:

| Modalità | Screenshot inviato al modello | `max_tokens` | Immagini in contesto |
|---|---|---|---|
| `normal` (default) | PNG a risoluzione piena | 1024 | max 10, si taglia a 5 |
| `hurry` | PNG ridotto al 75% | 512 | max 10, si taglia a 5 |
| `super_fast` | JPEG q60 ridotto al 50% | 384 | max 5, si taglia a 2 |

Lo screenshot viene ridimensionato **una volta sola**, dalla cattura a piena
risoluzione (ridurlo e poi lasciarlo ri-ingrandire a `smart_resize` costava più
byte dell'originale).

Se uno studio sta sforando il tempo minimo stimato la velocità **sale da sola**:
`hurry` appena si supera il minimo, `super_fast` dopo 2 minuti di sforamento.
L'escalation non scende mai di livello e, finito lo studio (o tornando
all'elenco), si ripristina la modalità che avevi scelto tu; nella GUI il bordo
giallo sul bottone indica proprio questo.

## Dashboard delle performance

`http://127.0.0.1:8766/dashboard` (o il bottone **📊** nella GUI) mostra: cicli
totali, completati (`CODE` / `NOCODE`), cicli senza esito (`NONE`), incompleti,
durata media per studio completato, cicli/ora, un istogramma dell'attività per
ora e la tabella degli studi completati con codice, durata, passi e **da dove
viene il codice** (URL o schermo).

La sezione **Dove va il tempo** divide il tempo tra elenco vuoto, studi
completati e cicli falliti, e i passi tra elenco, dentro uno studio e altrove su
Prolific, con la velocità recente del modello.

Ogni run scrive un `summary.json` accanto al transcript e la dashboard legge
quelli; per le run più vecchie il riassunto si calcola una volta e si salva. Si
aggiorna da sola ogni 60 s e, mentre l'agente gira, ha un bottone **⏹ Ferma**.

## Controllo da Telegram

Con `telegram_config.py` configurato (vedi [setup](#6-telegram-opzionale)) il
server fa polling dei comandi del bot. Tutti i comandi hanno anche la versione
italiana (`avvia`, `ferma`, `riavvia`, `stato`, `veloce`, `velocissimo`,
`aiuto`), con o senza `/`:

| Comando | Cosa fa |
|---|---|
| `/start [nome]` | avvia l'agente con il preset indicato (basta una parte del nome) o, senza nome, con il primo; non naviga, parte da dov'è il browser |
| `/presets` | elenca i preset salvati |
| `/stop` | ferma subito la run (come il tasto ■) |
| `/restart [nome]` | ferma, riporta il browser su `/studies`, riavvia |
| `/riepilogo` | il riepilogo della giornata (arriva comunque da solo alle 21, `FARA_SUMMARY_HOUR`) |
| `/status` | screenshot live + stato, uptime, ciclo/passi/form/errori, fase, URL, timer dello studio, modalità velocità, quanti studi sono in memoria come scartati, studi recenti e guadagni delle ultime 24 h (solo studi approvati) |
| `/inject <testo>` | manda un'istruzione extra alla run in corso: l'agente la riceve al passo successivo (come la casella in basso nella GUI) |
| `/normal`, `/hurry`, `/superfast` | cambiano [modalità di velocità](#modalità-di-velocità) |
| `/help` | la lista dei comandi |

Notifiche automatiche: 📝 studio iniziato (con la durata stimata e l'URL), ✅
studio completato (con il codice o `NOCODE`, e se il codice viene dall'URL),
⏭ studio scartato (motivo e link, se vuoi farlo a mano), ⚠️ codice da
verificare, ⚠️ ciclo fallito con il tempo del prossimo tentativo, 🐌 modello
lento, ❌ errore della run, 📊 riepilogo serale (cicli, studi completati e
scartati, errori, velocità del modello, guadagni approvati o in revisione).

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
| `FARA_SLOW_TPS` | `10` | sotto questa velocità di generazione (mediana di 6 risposte) arriva l'avviso "modello lento" |
| `FARA_SUMMARY_HOUR` | `21` | ora del riepilogo giornaliero su Telegram |
| `FARA_CAPTURE_DIR` | `var/captures` | dove salvare l'HTML delle pagine Prolific che servono per i test |
| `FARA_TEST_INSTANCE` | *(vuota)* | `1` su un secondo server di prova: niente bot Telegram (due poller sullo stesso token si rubano i messaggi) e niente lettura delle submission (aprirebbe una scheda nel browser vero) |
| `FARA_ENABLE_THINKING` | `0` | `1` = lascia il thinking del template. Sul 9B peggiora molto i click, sul 27B è indifferente |
| `FARA_STREAM_FPS` / `FARA_STREAM_QUALITY` | `5` / `60` | stream MJPEG della GUI |

## Log

Ogni run scrive in `var/logs/pw/run_AAAAMMGG_HHMMSS/`: `transcript.jsonl` (cosa ha
pensato/fatto a ogni passo) e `screenshot_NN.png`. Nel transcript, accanto alle
righe del dialogo col modello, ci sono righe `{"type": "diag"}` con lo stato
"meccanico" di ogni passo (URL, sito guardato sì/no, azione, risultato, stato del
timer, durata letta dalla card): servono per capire a posteriori perché il timer
è partito o no. La dashboard aggrega tutti i transcript presenti.

Oltre le ultime `FARA_MAX_RUNS` run (200) `var/logs/pw/` cancella gli screenshot
(sono loro a riempire il disco, uno per passo) ma comprime il transcript in
`transcript.jsonl.gz` e lo **tiene per sempre** (qualche decina di KB); dashboard
e conteggi leggono entrambe le forme, quindi lo storico non si perde.

Ogni run ha anche un `summary.json` (esito, da dove viene il codice, passi
sull'elenco / nello studio / altrove, velocità mediana del modello), e nel
transcript una riga per passo con i tempi di llama-server e, a fine studio, il
confronto tra il codice inserito e quelli visti (`code_check`).

Altri file in `var/logs/pw/`: `gui_server.out` (log del server quando è avviato dagli
script cloud o da systemd) e `submissions_api_debug.txt` (ultimo esito della
lettura delle submission di Prolific).

In `var/captures/` finisce l'HTML delle pagine Prolific vere che servono per i
test (elenco con studi, pagina di uno studio, redirect di completamento), una
volta al giorno per tipo. Contengono nome e saldo dell'account: si ritagliano e
si spostano a mano in `fixtures/`.

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
| `agent/tests_resilience.py` | guardrail, velocità, codici, attesa sull'elenco, recupero dei cicli: 117 controlli. Metà **offline** (modalità di velocità, veto del timer su `terminate`, rilevamento studi scartati e audio/video, memoria degli studi, rotazione dei log, backoff dei retry): `--offline` la esegue senza browser né rete, in qualunque momento. L'altra metà tocca il DOM e **si rifiuta di partire se una run è in corso** |
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
models/             modelli GGUF, ignorata da git, vedi "Modelli"
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
