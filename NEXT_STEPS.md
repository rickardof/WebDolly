# Next steps

Aggiornato il 19 ottobre dopo il rewrite Playwright dello sniffer Pusher,
il DOM nav hint, e la study memory cross-cycle.

---

## 1. Return dello studio quando l'agente si blocca

**Perché** — Uno studio preso e lasciato resta "attivo" fino al timeout del
ricercatore: il posto è occupato, e i timeout pesano sul profilo più di un
"Return" esplicito. Succede in tre casi oggi:

1. skip_av scatta **dentro** la survey (il `<video>` non era nella descrizione)
2. il ciclo si interrompe a metà (errore, scheda bloccata, budget superato)
3. lo studio sfora il budget di tempo/passi (#2 sotto)

**Come** — percorso di uscita hardcoded sul DOM: pagina dello studio su
Prolific → "Return" → conferma. Il modello come fallback se il layout cambia.
Notifica Telegram con motivo.

**Sforzo** — medio. Serve catturare l'HTML del flusso Return reale
(ispezionarlo a mano una volta).

## 2. Budget per studio in tempo e passi

**Perché** — run_20261013_101546: 116 passi dentro un task esterno senza
finire. Il preset ha max_rounds alto, niente lo ferma.
Il DOM nav hint e la study memory riducono i passi "normali", ma non prevengono
i loop infiniti.

**Come** —
- budget = 3× la durata dichiarata (dal DOM dell'elenco studi), minimo 8 min
- hard cap di passi: ~60 per studi da ≤5 min, ~120 per studi più lunghi
- allo scatto → return dello studio (#1) + notifica

**Dipende da** — #1 (return flow).

## 3. Rilevamento "fermo" (stuck detection)

**Perché** — Il modello a volte ripete la stessa azione per decine di passi
senza che la pagina cambi. Il budget in tempo (#2) alla fine lo ferma, ma
brucia passi e token inutilmente.

**Come** —
- hash percettivo dello screenshot: se invariato per N passi consecutivi
  (es. 5), inject di un hint nel prompt ("la pagina non è cambiata, prova
  un'azione diversa o termina")
- se invariato per 2N passi → abort + return (#1)
- stesso approccio per azione identica ripetuta (stessa action_type + args)

**Sforzo** — piccolo. `imagehash` o un semplice crop+hash della zona centrale.

## 4. Screen-out detection dentro la survey

**Perché** — Quando uno studio fa screen-out **dentro** la survey esterna
(non su Prolific), l'agente oggi non lo registra in study_cache. Il rientro
su Prolific con stato "screened out" subito dopo è un segnale pulito.

**Come** —
- dopo ogni studio, se Prolific mostra "returned/screened out" invece di
  "completed", registrare in study_cache con motivo
- combinabile con study_memory: se un dominio screen-outta >50% delle volte,
  aggiungere un hint di cautela

**Sforzo** — piccolo-medio.

## 5. Tasso di approvazione e guadagno reale

**Perché** — La dashboard mostra dove va il tempo, ma non quanti studi vengono
approvati/rifiutati. Serve per capire se le modifiche migliorano il risultato
netto.

**Come** —
- leggere la pagina submissions periodicamente (o dopo ogni studio)
- registrare approvazione/rifiuto/pending per ciascuno
- nella dashboard: tasso di approvazione, guadagno cumulativo, £/h effettivo

**Sforzo** — medio. Il parsing del DOM submissions è fragile (già rotto una
volta per cambio layout).

## 6. Watchdog del container browser

**Perché** — La scheda bloccata si recupera (BrowserStateError + recycle_tab),
il container no. Se il container muore, l'agente si blocca su pw_bridge
timeout senza recovery.

**Come** —
- CDP muto per N tentativi → `docker restart fara-browser-pw` + ripartenza
- indicatore "browser non risponde" nella barra GUI

**Sforzo** — piccolo.

---

## Pulizia rimasta

- [ ] `data/` alla radice: cartella vuota di root, avanzo vecchio stack → `sudo rm -rf data`
- [ ] container `fara-browser` (pre-Playwright) fermo → `docker rm fara-browser`
- [ ] `agent/survey_loop.py` non importato da nessuno → cancellare
- [ ] `var/attic/` vecchi screenshot di prova → `rm -rf var/attic`

## Fatto (questa sessione)

- **Sniffer Pusher riscritto** — da CDP (rotto: webSocketCreated non vede WS
  pre-esistenti) a Playwright `page.on("websocket")`. Rimosso `_ws_live`
  inaffidabile (close non triggera su navigazione).
- **Event pumping** — `_wait_for_studies` ora pompa il loop Playwright con
  `wait_for_timeout(200)` tra ogni check, altrimenti i callback WS non
  scattavano mai.
- **DOM nav hint** — `_dom_nav_hint()` trova i bottoni Next/Submit/Continue
  su tutti i frame (qualsiasi profondità) e passa le coordinate al modello.
- **Study memory cross-cycle** — `study_memory.py` ricorda quali domini
  survey usano `run_js`, e lo suggerisce nei cicli futuri.
