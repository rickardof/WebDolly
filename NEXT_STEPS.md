# Next steps

Stato al 13 ottobre 2026. Ordinato per urgenza, non per dimensione.
Quello che è stato chiuso è in fondo, in [Fatto](#fatto), con una riga su come
verificarlo.

---

## 1. Ruotare i segreti (manuale, da fare una volta)

I file sono usciti dalla repo (`.gitignore` + `.example`), ma **restano nella
storia di git** e sono già stati pushati su `origin/main`. Il repo è privato,
quindi non è un incidente; i due valori però sono ancora validi.

- [ ] token del bot: `/revoke` su @BotFather, poi riscrivere
      `agent/telegram_config.py`
- [ ] chiave dell'API del modello: rigenerarla e riscrivere `scripts/cloud/fara.env`
- [ ] solo se il repo diventa pubblico: riscrivere la storia
      (`git filter-repo --invert-paths --path scripts/cloud/fara.env --path
      agent/telegram_config.py`) e force-push

## 2. Far girare la metà DOM dei test

`agent/tests_resilience.py` esegue 54 controlli offline, ma la metà che
tocca il browser (bottone disabilitato, recupero del click su checkbox, durata
letta dalla card giusta, `<video>` in pagina, chiusura delle schede extra) non è
mai stata eseguita: si rifiuta di partire con una run in corso, com'è giusto.

- [ ] il container di prova è già pronto (fermo, CDP :9334): il comando esatto
      è nel README, sezione "Test e benchmark"
- [ ] `docker start fara-browser-test` e lanciarlo
- [ ] aggiungere i casi che restano scoperti: `terminate` rifiutato dentro una
      run vera, e la riga della cache iniettata nel prompt sull'elenco studi

## 3. Watchdog del browser

La scheda bloccata ora si recupera da sola (`pw_bridge.recycle_tab`), ma il
container no: se muore Chromium, ogni tentativo fallisce allo stesso modo
finché non intervieni a mano.

- [ ] se il CDP non risponde per N tentativi, `docker restart` del container e
      ripartenza da `/studies`
- [ ] notifica Telegram distinta ("browser riavviato"), per distinguerla da un
      errore del modello
- [ ] `/screen.png` passa da `scrot`, quindi mostra l'ultimo frame disegnato
      anche quando il renderer è bloccato: la GUI sembra viva mentre non lo è.
      Un segnale di "pagina non risponde" nella barra userebbe il dato che ora
      c'è già (URL illeggibile)

## 4. Telegram: scegliere il preset

`_do_start()` usa `presets[0]`, quindi dal telefono si lancia un solo profilo di
lavoro. `/inject` invece ora c'è.

- [ ] `/start <nome>` con match parziale sul nome del preset
- [ ] `/presets` per listarli (o una inline keyboard con un bottone per preset)

## 5. Lettura delle submission fragile

`_fetch_submissions()` fa regex sull'`innerText` della pagina Prolific: dipende
da lingua, layout e stringhe come `AWAITING REVIEW`. Se cambia qualcosa torna
zero righe e `/status` dice "nessuna submission" senza spiegare perché (il
motivo finisce in `var/logs/pw/submissions_api_debug.txt`).

- [ ] salvare l'HTML in `fixtures/test/` e scrivere un test offline del parser
      (nessun browser, nessuna rete: come la metà offline di `tests_resilience`)
- [ ] leggere il DOM strutturato (righe della tabella) invece del testo
- [ ] se il parsing torna vuoto ma la pagina ha caricato, mandare un warning su
      Telegram invece di restare in silenzio

## 6. Cloud: verificare davvero su ARM

I path sono stati uniformati (`FARA_HOME`), ma niente è stato provato su una VM.

- [ ] confermare che `browser/Dockerfile` (bookworm + chromium + scrot +
      xdotool) builda e fa screenshot su arm64
- [ ] un ciclo completo end-to-end col modello via API, salvando il risultato in
      `bench/bench_models/` come gli altri (oggi i bench coprono solo 27B e 9B locali)
- [ ] `FARA_MODEL=gemini-2.0-flash` è vecchio: valutare un modello recente e
      rifare il confronto grounding/batch-fill con `model_bench.py`

## 7. Conteggio token approssimato con le API cloud

Senza `llama-server` non c'è `/tokenize`, quindi `_tokenize_count()` stima
~4 caratteri per token. È conservativo sul testo ma non vale per le immagini, e
`MEASURED_IMAGE_TOKENS = 1262` è tarato su Fara: con un altro modello il budget
di contesto può sforare, e con le API si paga a token.

- [ ] misurare quanto costa in token un'immagine sul provider usato e aggiornare
      la costante (o renderla una env var)
- [ ] usare l'endpoint di conteggio del provider quando esiste
- [ ] loggare i token effettivi di ogni chiamata nel transcript, così il consumo
      si vede nella dashboard

## 8. Dashboard: ricalcolo completo a ogni richiesta

`_build_dashboard()` rilegge e riparsa *tutti* i transcript a ogni GET (ora anche
quelli compressi), e la pagina si auto-ricarica ogni 60 s. Su una VM piccola è
lavoro inutile su un server single-process che serve anche lo stream MJPEG.

- [ ] cache del risultato invalidata dall'evento `done`
- [ ] o un `summary.json` per run, scritto a fine ciclo
- [ ] mostrare i guadagni (già letti per Telegram) accanto ai cicli/ora
- [ ] mostrare gli studi in memoria come scartati (oggi solo via
      `GET /study_cache`)

## 9. Code di pulizia dopo il trasloco

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

- `hurry` manda ancora PNG: su uno screenshot vero sono ~380 KB contro i ~50 KB
  del JPEG di `super_fast`. Irrilevante col modello in locale, non con un'API a
  pagamento: valutare JPEG q80 anche per `hurry`.
- Gli screen-out che avvengono **dentro** la survey esterna non entrano nella
  memoria degli studi scartati (di proposito: lì le stesse frasi possono essere
  una domanda). Si potrebbe riconoscere il rientro su Prolific con stato
  "screened out" subito dopo, che è un segnale pulito.
- Riconoscere gli studi che sforano sistematicamente il tempo dichiarato e
  declassarli, invece di sceglierli solo per ricompensa.

---

## Fatto

- **Segreti fuori dalla repo** — `agent/telegram_config.py` e
  `scripts/cloud/fara.env` sono ignorati da git, con `.example` commentati al loro
  posto; resta solo da ruotare i valori (§1). Verifica:
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
