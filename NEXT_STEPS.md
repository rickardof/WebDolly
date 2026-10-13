# Next steps

Ordinati per impatto. I numeri vengono dai log del 13 ottobre (08:44 → 14:10):

| | |
|---|---|
| tempo sull'elenco vuoto | 72% (196 cicli su 211 erano "niente da fare") |
| tempo negli studi completati | 5% — 3 studi, mediana 36 passi, 5 min |
| tempo in cicli falliti | 23% — uno da 127 passi su un task esterno, mai finito |
| velocità del modello | ~33 token/s sano; 17 e poi 2,4 dopo ore di cache riusata |

---

## 1. Fermare uno studio che non va da nessuna parte

**Perché** — `run_20261013_101546`: 116 passi dentro un task esterno senza
arrivare in fondo; il preset ha `max_rounds: 1500`, quindi niente l'avrebbe
fermato. È la fetta più grossa del 23% di tempo nei cicli falliti.

- [ ] budget per studio in tempo e passi, proporzionato alla durata dichiarata
      (es. 3× la stima, con un minimo), separato da `max_rounds`
- [ ] rilevamento "fermo": stesso screenshot (hash percettivo) per N passi, o la
      stessa azione ripetuta senza che la pagina cambi
- [ ] allo scatto, il punto 2

Lo stato c'è già (`StudyTimer`, `_current_study_id`, `summary.json` dice già
quanti passi finiscono "dentro uno studio"). **Sforzo**: piccolo.

## 2. Restituire lo studio invece di abbandonarlo

**Quando va restituito** — ogni volta che l'agente ha *iniziato* uno studio
(ha cliccato "Take part", su Prolific lo studio risulta tuo) e poi smette senza
finirlo. Oggi succede in tre casi, e in tutti l'agente naviga via e basta:

1. con "salta studi con audio o video" attivo, il `<video>` compare **dentro**
   la survey, quando lo studio è già preso;
2. il ciclo si interrompe a metà studio (errore, scheda bloccata, `max_rounds`);
3. con il punto 1, lo studio si blocca o sfora il budget.

Non serve invece quando lo studio viene scartato **prima** di prenderlo (non
idoneo, pieno, audio/video letto nella descrizione): lì non c'è niente da
restituire.

**Perché conta** — uno studio preso e lasciato resta "attivo" fino al timeout
del ricercatore: il posto resta occupato per tutto quel tempo, e i timeout
pesano sul profilo più di un "Return" esplicito.

- [ ] percorso di uscita: pagina dello studio su Prolific → "Return" → conferma,
      fatto in codice sul DOM, con il modello solo come ripiego
- [ ] usarlo nei tre casi sopra, con notifica Telegram e motivo
- [ ] serve l'HTML vero di quel flusso (vedi punto 4)

**Sforzo** — medio.

## 3. Tasso di approvazione e guadagno reale

La dashboard ora dice dove va il tempo, ma non quanti studi vengono approvati
o rifiutati: quello viene dalla pagina delle submission, letta con regex
sull'`innerText`, che si azzera in silenzio al primo cambio di layout.

- [ ] HTML della pagina submission in `fixtures/` + test offline del parser
- [ ] lettura delle righe della tabella invece del testo
- [ ] approvati / in revisione / rifiutati / timeout nella dashboard e nel
      riepilogo serale
- [ ] avviso su Telegram quando la pagina carica ma il parsing torna vuoto

## 4. Fixture Prolific vere

L'agente ora salva da solo l'HTML delle pagine utili in `var/captures/` (elenco
con studi, pagina di uno studio, redirect di completamento), una volta al giorno
per tipo. Resta da:

- [ ] ritagliarle (via nome e saldo) e spostarle in `fixtures/`
- [ ] catturare anche il flusso di "Return" e la pagina submission (servono ai
      punti 2 e 3)
- [ ] test DOM su quelle pagine: lettura dell'elenco con studi veri, codice di
      completamento sulla pagina vera

## 5. Evitare che la cache del modello si degradi di nuovo

Il 13 ottobre la generazione è scesa da ~33 a 17 token/s alle 12:51 e a 2,4 alle
13:58, mentre una richiesta nuova andava ancora a 24-33: si era degradata la
cache dei prompt dello slot che l'agente riusava da ore. `docker restart
fara-test` l'ha risolta; ora arriva un avviso 🐌 se ricapita. Per evitarlo:

- [ ] riavviare llama-server con `--slot-save-path`: abilita
      `POST /slots/<id>?action=erase`, e l'agente può svuotare la cache del suo
      slot a inizio ciclo (ogni ciclo riparte comunque da una conversazione
      nuova, quindi non si perde niente)
- [ ] in alternativa, riavvio automatico del container quando scatta l'avviso
      (più brutale, da decidere)
- [ ] tenere d'occhio la velocità nei prossimi giorni: ora è nel `summary.json`
      di ogni run e nella dashboard

## 6. Ricostruire le immagini del browser

Il fix al lock del profilo (`docker/browser/entrypoint.sh` cancella i
`Singleton*` vecchi all'avvio) entra solo con le immagini ricostruite. Finché
non si fa, ogni container ricreato resta senza CDP finché qualcuno non toglie il
lock a mano, come è successo due volte il 13 ottobre.

- [ ] `docker build -t fara-browser docker/browser/` e
      `docker build -t fara-browser-pw docker/browser-pw/`, poi ricreare
      `fara-browser-pw` e `fara-browser-test` (comandi nel README); con l'agente
      fermo

---

## Affidabilità e infrastruttura

### 7. Watchdog del container del browser
La scheda bloccata si recupera da sola, il container no.
- [ ] CDP muto per N tentativi → `docker restart fara-browser-pw` e ripartenza
- [ ] nella barra della GUI un segnale "pagina non risponde" quando l'URL non si
      legge: lo screenshot passa da `scrot` e sembra vivo anche quando non lo è

### 8. Cloud (solo se si passa a un modello via API)
- [ ] build dell'immagine del browser su arm64 e un ciclo completo sulla VM
- [ ] bench del modello remoto in `bench/bench_models/`, come gli altri
- [ ] conteggio token vero (oggi stima ~4 caratteri/token; l'immagine è tarata
      su Fara)

## Pulizia rimasta dal trasloco

La repo è stata riorganizzata (vedi [Fatto](#fatto)); restano due residui che
richiedono una tua decisione.

- [ ] `data/` alla radice: cartella vuota di proprietà di root, avanzo del
      vecchio stack. Serve `sudo rm -rf data` (io non posso)
- [ ] il container `fara-browser` (stack pre-Playwright) è fermo e i suoi mount
      ora puntano a percorsi che non esistono più. Se non ti serve:
      `docker rm fara-browser`, e poi `rm -rf var/profiles/legacy` (311 MB di
      cookie e login di quel browser, irreversibile)
- [ ] `agent/survey_loop.py` non è importato da nessuno (la pipeline attuale è
      `form_fill_pw.py`; `form_fill.py` invece serve ancora): si può cancellare,
      la storia di git lo conserva
- [ ] `var/attic/` contiene i vecchi screenshot di prova e `fara-test-1440x900.png`:
      `rm -rf var/attic` quando vuoi

## Idee, non ancora decise

- Gli screen-out che avvengono **dentro** la survey esterna non entrano nella
  memoria degli studi scartati. Il rientro su Prolific con stato "screened out"
  subito dopo è un segnale pulito per registrarli.
- Riconoscere dall'elenco (DOM) quali studi sono già nella memoria degli
  scartati, e non chiamare il modello se l'elenco contiene solo quelli.

## Fatto

- **Attesa sull'elenco senza modello** — tra un ciclo e l'altro la pagina viene
  ricaricata e letta dal DOM ogni ~45 s (la cadenza che il loop aveva già); il
  modello parte solo se c'è qualcosa, se la pagina non è riconoscibile o ogni
  15 minuti. Verificato sulla pagina vera ("Waiting for studies" → `empty`).
- **Codice di completamento** — dall'URL del redirect Prolific (`?cc=`) in
  codice; quando il codice è sulla pagina dello studio (testo, campo da copiare,
  iframe, URL della pagina) viene annotato durante lo studio e confrontato con
  quello che il modello inserisce su Prolific: ⚠️ se non torna o se chiude con
  `NOCODE` mentre un codice c'era. Gli studi senza codice restano come prima.
- **Metriche** — `summary.json` per run (esito, da dove viene il codice, dove
  sono andati i passi, velocità del modello); la dashboard li legge invece di
  riparsare tutto, e mostra "Dove va il tempo" e la colonna "Codice da".
- **Notifiche** — studio scartato con link per farlo a mano, codice da
  verificare, modello lento, riepilogo serale alle 21 (`/riepilogo` a richiesta),
  `/presets`, `/start <nome>` e `/restart <nome>`.
- **Velocità del modello** — i tempi di llama-server si registrano a ogni
  risposta (transcript, `/agent/status`, dashboard) con avviso sotto i 10
  token/s. La misura ha trovato il vero collo di bottiglia: cache dei prompt
  degradata (2,4 token/s invece di 33), risolta riavviando `fara-test`. L'idea
  di mandare JPEG anche in `hurry` è caduta: a parità di risoluzione i token
  dell'immagine sono gli stessi, cambiano solo i byte su localhost.
- **Bug nel recupero dei cicli** — un `import traceback` dentro `agent_driver`
  rendeva il nome locale a tutta la funzione, e il ramo che doveva riprovare
  dopo 30 s andava in crash con `UnboundLocalError`: è successo davvero, al
  riavvio del modello, e ha fermato la run. Corretto, con un test che esegue
  tutto il percorso di recupero e che col codice vecchio fallisce.
- **Metà DOM dei test eseguita** per la prima volta sullo stack isolato:
  `tests_resilience.py` 117/117.
- **Segreti fuori dalla repo** — `agent/telegram_config.py` e
  `scripts/cloud/fara.env` sono ignorati da git, con `.example` commentati al loro
  posto. Verifica:
  `git check-ignore -v agent/telegram_config.py scripts/cloud/fara.env`.
- **Path di `scripts/cloud/` uniformati** — tutti gli script usano `FARA_HOME`
  (default `~/web-agent`), `sync-to-cloud.sh` ricava la sorgente da dove si
  trova, il service systemd prende i path dall'`EnvironmentFile` e logga nel
  journal. Verifica: `bash -n scripts/cloud/*.sh`.
- **Un ciclo in errore non ferma più la run** — retry con backoff 30 s → 1 →
  2 → 5 min, evento `error` nel feed, notifica Telegram con il traceback e
  ritorno alla pagina di partenza prima di riprovare. Solo le run singole (non
  in loop) falliscono subito, perché lì non c'è un ciclo dopo. Verifica:
  `tests_resilience.py` (`_backoff`).
- **Timer e velocità nella GUI** — contatore `3:41 / 12:00` giallo/verde nella
  barra, tre bottoni per la modalità, bordo giallo quando è stata alzata dal
  timer; `POST /agent/speed` e `speed`/`study_timer` in `/agent/status`.
- **`/inject <testo>` su Telegram** — più il fix al parser dei messaggi, che
  andava in crash (e silenziava il bot) su qualunque messaggio senza testo,
  tipo una foto.
- **Memoria degli studi scartati** — `agent/study_cache.py`, i titoli
  finiscono nel prompt sull'elenco studi, scadenza 14 giorni, API
  `GET /study_cache`, `POST /study_cache/{clear,forget}`.
- **Opzione "salta studi con audio o video"** — interruttore nella GUI, campo
  `skip_av` nei preset e in `/start_agent`: media element o player su qualunque
  pagina, frasi tipo "headphones required" solo sulla descrizione dello studio.
- **Rotazione dei log senza perdere lo storico** — oltre `FARA_MAX_RUNS` (200)
  gli screenshot si cancellano e il transcript si comprime in `.jsonl.gz`;
  dashboard e conteggi leggono entrambe le forme.
- **`requirements.txt`** — `playwright`, `pillow` (+ `websocket-client` solo per
  il vecchio percorso `cdp.py`); `setup-vm.sh` lo usa se c'è.
- **`FARA_TEST_INSTANCE=1`** — un secondo `gui_server` (lo stack di test sulla
  8767) non fa più polling Telegram sullo stesso token della live e non apre
  schede nel browser per leggere le submission.
- **Scheda del browser bloccata** — se il renderer si impalla, Playwright
  riporta `page.url == ''` e ogni `evaluate()` resta appeso fino al timeout,
  mentre lo schermo continua a mostrare l'ultimo frame: l'agente andava avanti
  alla cieca (niente guardrail, un passo ogni cinque minuti). Ora un URL
  illeggibile fa fallire il ciclo con `BrowserStateError` e il recupero
  sostituisce la scheda via CDP HTTP, che risponde anche quando Playwright no.
- **Repo riorganizzata** — `agent/` (ex controller_pw), `docker/browser{,-pw}/`,
  `fixtures/{pw,test}/` (le pagine di prova montate come `/data`), `bench/`,
  `scripts/` (lanciatori + `cloud/` + `tools/`) e `var/` per tutto il runtime
  (log e profili del browser), ignorata da git. Gli script e il codice ricavano
  i percorsi da dove si trovano, quindi non c'è più nessun
  `/home/riccardo/web-agent` scritto dentro; i due container del browser sono
  stati ricreati con i nuovi mount e il profilo (login Prolific) è intatto.
- **Un bug trovato dai test** — in `hurry` lo screenshot veniva ricampionato due
  volte (prima ridotto, poi riportato in parte su da `smart_resize`): mandava
  411 KB contro i 376 KB di `normal`, con l'immagine più sfocata. Ora il resize
  è uno solo, dalla cattura a piena risoluzione.
