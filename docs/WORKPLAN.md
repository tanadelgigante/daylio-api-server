# WORKPLAN — Daylio API Server: evoluzione completa

**Percorso:** `docs/WORKPLAN.md` (piano di lavoro nella cartella documentazione del progetto)
**Utente / Orchestratore:** **Approvato 2026-09-22** (confermati punti aperti §11)
**Data stesura:** 2026-09-22 (aggiornato 2026-09-22: conferme §11, config YAML, APScheduler, rinomina `db/migrations.py`, `vars.INFLUX_INSTANCE`)
**Autore:** Designer (architetto software)
**Stato:** **APPROVATO 2026-09-22** — tutti i punti aperti del §11 sono RISOLTI con le conferme dell'utente: D15/D15b immagine e tag, build solo su push, Chart.js, auth sessioni + API key, Influx org `tanadelgigante`/bucket `daylio`/`vars.INFLUX_INSTANCE` per le prove CI, release solo manuale via `workflow_dispatch`, rinomina `db/migrations.py`, scheduler **APScheduler**. Config applicativa migrata da TOML a **YAML** (`PyYAML`, richiesta esplicita dell'utente) con esempi reali `config/config.example.yml` e `config/users.example.yml`.

---

## 1. Obiettivo e contesto

Il progetto `daylio-api-server` è oggi un **singolo file `server.py`** (147 righe, Flask + pandas + SQLite, logging con `print`, nessun test, nessuna CI) che importa un CSV di export dell'app **Daylio** da una cartella `data/` e lo espone via una manciata di endpoint REST.

L'obiettivo è un'**evoluzione completa** che trasformi il progetto in un servizio multiutente, configurabile, con doppio storage (SQLite = fonte di verità, InfluxDB = serie storiche per dashboard/analytics), import unificato CSV + `.daylio`, API parametrica, dashboard integrata e automazione completa (Gitea Actions, pytest, ruff, docs/wiki, logging a livelli, `version.ini`, notifiche Ntfy) sul modello del progetto di riferimento `flatpress-publish-article-plugin` e del processo build&push di `infrastructure-tools`.

**Esito finale atteso:** server API testato e automatizzato (CI verde), dati giornalieri univoci con upsert, tutte le colonne/scales configurabili ed esposte, dashboard che legge **solo** da InfluxDB, immagine Docker multi-arch pubblicata (pattern infrastructure-tools), documentazione completa (README inglese, WORKPLAN italiano, wiki o `docs/`).

---

## 2. Stato attuale / Baseline

### 2.1 Repo e file
| Voce | Valore |
| --- | --- |
| Branch | `main` (origin/main presente) |
| Ultimi commit | `3cbc9e0 data.db` · `ac30ead fixz req` · `976a537 complete csv format` · `b166ce2 first version (ai only)` · `db749ec Initial commit` |
| File tracciati | `server.py`, `requirements.txt` (`flask`, `pandas`), `Dockerfile` (`python:3.9`, single-stage), `docker-compose.yml` (porta host `3177:5000`, volumi `./data` e `./data.db`), `README.md` (in italiano), `LICENSE` |
| Worktree | Modifiche non committate: `server.py`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`; file untracked: `.gitignore` (contiene `sampledata/*`), `sample-data/` |
| `data.db` | **Committato erroneamente** in git (`3cbc9e0 data.db`) → da rimuovere dai tracciati in Fase 0 |
| Sample reali | `sample-data/daylio_export_2026_09_22.csv` (630 righe, BOM UTF-8) e `sample-data/backup_2026_09_22.daylio` (ZIP → `backup.daylio` Base64 → JSON 136KB) |

### 2.2 Analisi del codice attuale (server.py)
- **Unico file**, `DB_PATH="data.db"`, `DATA_FOLDER="data"`.
- **`MOOD_MAP` INVERSO rispetto a Daylio**: `{terribile:0, male:1, così così:2, buono:3, ottimo:4}` (0=migliore, 4=peggiore) mentre la convenzione Daylio è **1 = migliore … 5 = peggiore** → **bug pre-esistente da correggere** (D1).
- **Dedupe attuale per `(user, full_date, time)`**: crea 2 record per lo stesso giorno se esiste più di un `time` → **viola R5** (1 record per giorno, chiave `(user, full_date)`).
- Logging con `print`, import a thread con `wait_until_start_hour` + `IMPORT_INTERVAL` (env).
- Endpoint minimi: `GET /users`, `GET /moods?user&start_date&end_date`.
- Dipende da **pandas** (da rimuovere, D5) e da Flask non pinnato.

### 2.3 Dati rilevati dai campioni reali
**CSV `daylio_export_2026_09_22.csv`** (letti con `utf-8-sig` per BOM):
- Header: `full_date,date,weekday,time,mood,activities,scales,note_title,note` — 630 righe dati.
- `full_date` = `YYYY-MM-DD` (chiave giorno); `date`/`weekday` derivabili da `full_date` (default D8.a).
- `mood` = solo etichette testuali, 5 valori: `alla grande`(31), `bene`(282), `così così`(174), `male`(119), `malissimo`(24).
- `activities` = lista `" | "` (39 attività distinte); tra queste compaiono etichette della **Scala salute** (`discreto`, `insufficiente`, `mediocre`, `pessimo`, `sufficiente`, …) → **la scala salute nel CSV va estratta dalle `activities` secondo config, NON dalla colonna `scales`** → D2.
- `scales` presente in **58/630** righe, formato `Stress: Moderato 3/5` → 5 livelli nominati (Calma, Basso, Moderato, Alto, Sopraffatto).
- `note_title`/`note` sempre vuote nel campione (opzionali).
- **2 giorni duplicati**: `2025-01-01` (15:44, 20:02) e `2025-01-03` (14:03, 21:36) → serve regola "vince la più recente".

**File `.daylio` `backup_2026_09_22.daylio`** (ZIP; dentro `backup.daylio` Base64 → JSON, version 15, 630 `dayEntries`):
- `customMoods`: 5 oggetti `{id 1..5, mood_group_id 1..5, …}` con **convenzione APA: 1 = migliore ("alla grande"), 5 = peggiore ("malissimo")**. Mapping id JSON → etichette CSV: `1=alla grande, 2=bene, 3=così così, 4=male, 5=malissimo`.
- `tags`: 49 attività; `tag_groups`: 6 gruppi (1=Emozioni, 3=Salute, 2=Faccende, 4=Stato salute, **5=Scala salute**, 6=Sonno). Il gruppo 5 contiene i livelli `ottimo, buono, discreto, sufficiente, mediocre, insufficiente, pessimo, terribile` (ordine di scala definito dall'utente, da configurare → **non fidarsi del campo `order` JSON**).
- `scales`: 1 scala `{id:1, type:0, name:"Stress", text_scale:{values:[Calma…Sopraffatto]}, number_scale:{min:1,max:10}}` → supporto scale **testuali** (livelli nominati) e **numeriche** (min/max).
- `dayEntries[]`: `{id, year, month(0-based!), day, hour, minute, datetime(epoch ms), timeZoneOffset, mood(FK), tags(FK array), assets[], isFavorite, scaleValues[{id_scale, id_text_scale_value}], note, note_title}`. `scaleValues` solo per `id_scale=1` (Stress) in 58/630 entry, combaciano con la colonna `scales` del CSV.
- **Scala salute NON in `scaleValues` ma nei `tags`** (id_tag_group=5) → anche per `.daylio` va mappata dai tags secondo config → D2.
- `assets`: array media/foto (vuoto nel campione) → **D11 (default: ignorate)**.
- `metadata` {number_of_entries, platform, android_version, backup_version…}; `prefs`; `goals`/`goalEntries`/`goalSuccessWeeks` vuoti; `writingTemplates`; `reminders`.

---

## 3. Requisiti

### 3.1 Funzionali
- **R1. Colonne e scale configurabili.** Le colonne del record e le relative scale di valori (mood, socia del mood 5 livelli, eventuali scale custom come Stress, Scala salute) NON sono hardcoded: sono definite in config (YAML con PyYAML + overrides per utente).
- **R2. API parametrica su tutte le colonne custom.** Endpoint REST che permettono **selezione colonne** e **filtri** (per data, utente, scale). Supporto tutte le colonne personalizzate (mood, activities, scales, note, ecc.).
- **R3. Esportazione su InfluxDB esistente.** Scrittura dei contenuti su un InfluxDB 2.x "già esistente" che l'utente ha fornito in docker-compose (`image influxdb:latest`, porta host **8120:8086**, volumi `/var/influx/influxdb:/var/lib/influxdb2` e `/var/influx/config:/etc/influxdb`, `restart: always`). Auth via token.
- **R4. Dashboard grafica integrata che legge SOLO da InfluxDB.** La dashboard (SPA servita dal server) legge esclusivamente da InfluxDB, mai dal DB SQL (decisione utente).
- **R5. Import UNIVOCI per giorno.** Per il giorno X c'è UN SOLO record per utente: chiave `(user, full_date)`; se l'utente ha modificato un giorno nel passato → **UPDATE (upsert)**.
- **R6. MULTIUTENTE.** Utenti creati da admin; password admin nelle env; ogni utente ha una directory di import **configurabile**; isolamento dei dati per utente.
- **R7. Due modalità di import.** (a) **upload** di file (CSV o `.daylio`) via Web UI/API; (b) **cartella di import per utente** (configurabile) con import automatico **periodico configurabile** (intervallo + ora di inizio).
- **R8. Formato di import alternativo `.daylio`.** Archivio ZIP che contiene `backup.daylio` (Base64) → JSON con l'intera struttura del diario (customMoods, tags, scales, dayEntries…).

### 3.2 Non funzionali / tecnici / manutenibilità
- **R9. Automazione sul modello flatpress**: test (pytest), workflow Gitea Actions completo, documentazione, wiki, commenti al codice, logging a livelli DEBUG/INFO configurabili, stampaggio `[skip-ci]`, `version.ini`, notifiche Ntfy. **In più** (integrazione 2): job **build&push immagine Docker multi-arch** sul modello di `infrastructure-tools` (guardia `[skip]` sul commit, `alpine:3.23` + docker-cli/buildx/qemu, CA interna, matrix immagini, login via stdin, push multi-platform).
- **R10. Stack lean.** Python 3.12+, niente pandas (parser CSV stdlib), niente gunicorn (WSGI puro: Waitress), dipendenze ridotte e pinnate (requirements con lock/hash), Flask 3.x, SQLite (migrazioni versionate semplici, niente ORM pesante; SQLAlchemy Core opzionale), config YAML (`PyYAML`) + env per segreti, client ufficiale `influxdb-client`, Chart.js vendored per la dashboard.
- **R11. Qualità.** pytest con fixture reali dai `sample-data`, isolamento, failOnWarning-like; lint ruff; struttura a package (non single-file).
- **R12. Lingua e documentazione.** README in inglese; WORKPLAN e docs di lavoro in italiano; commenti al codice in inglese con docblock "MAINTENANCE NOTES" dove rilevante; wiki Gitea prevista (fallback: gerarchia di Markdown in `docs/`).

---

## 4. Vincoli e decisioni prese (D1..D18)

> Le decisioni D1–D12 erano già state prese con l'utente nella sessione precedente; **D13** è introdotta dall'Integrazione 1 (struttura repository); **D14–D16** dall'Integrazione 2 (workflow build/push); **D17–D18** dalle conferme 2026-09-22 (config YAML, Influx/APScheduler).

- **D1 — Convenzione mood 1=best … 5=worst (correzione bug).** Nuovo mapping default: `alla grande=1, bene=2, così così=3, male=4, malissimo=5` (etichette configurabili per utente). Sostituisce il `MOOD_MAP` invertito attuale. Range 1–5.
- **D2 — "Scala salute" = scala ordinata di 8 livelli, estratta dalle attività/tags.** Livelli: `ottimo, buono, discreto, sufficiente, mediocre, insufficiente, pessimo, terribile`. L'ordine di scala è definito in config (non nel campo `order` JSON/CSV). Nel CSV va estratta dalle `activities` (pattern di livello della scala salute); nel `.daylio` va mappata dai `tags` (id_tag_group=5); **non** dalla colonna `scales`/`scaleValues`. Le altre activities restano una lista senza imposizione di gruppi.
- **D3 — Unicità `(user, full_date)` + "vince la più recente".** Un record per giorno per utente. In caso di più entry nello stesso giorno si tiene la più recente per `time` (poi per `datetime`/id). **Upsert completo** sia su SQLite (`ON CONFLICT DO UPDATE`) sia su Influx (overwrite del punto giornaliero).
- **D4 — Doppio storage (modello a due sistemi).** **SQLite** = fonte di verità dei record + config/account/audit e garanzia di unicità. **InfluxDB 2.x** = serie storiche per dashboard/analytics (la dashboard legge SOLO da Influx). Scrittura coordinata: SQLite commit OK → write Influx (con strategia overwrite D3/§5.4). Prevedere ri-sincronizzazione (job di resync Influx da SQLite).
- **D5 — Stack lean.** Python 3.12+, `csv` stdlib (niente pandas), Flask 3.x, Waitress (WSGI), `influxdb-client`, config YAML (`PyYAML` — richiesta esplicita dell'utente) + env per segreti, ruff+pytest, dipendenze pinnate (requirements.lock/pip-tools), **APScheduler** per i job periodici. Niente ORM pesante.
- **D6 — Multiutente + import per cartella + scheduler.** Utenti creati solo da admin; `ADMIN_USERNAME`/`ADMIN_PASSWORD` in env; hash PBKDF2 (Werkzeug); sessioni firmate (itsdangerous) **per Web UI** + **API key per script automatici**; decorator admin. Directory di import configurabile per utente (admin/config). Due modalità (R7); scheduler periodico per-utente (intervallo, ora inizio) basato su **APScheduler** (non threading nativo — confermato 2026-09-22).
- **D7 — Formato `.daylio` (ZIP + Base64 + JSON).** Rilevamento formato tramite magic bytes (ZIP `PK`). Mapping da JSON: customMoods (1=best…5=worst), tags+tag_groups, scales (text_scale + number_scale), dayEntries (month 0-based, FK mood, tags, scaleValues, assets).
- **D8 — Dettagli normalizzazione:**
  - **D8.a** `date`/`weekday` derivati da `full_date` (default); ridondanti nel CSV, ignorati.
  - **D8.b** Unica colonna obbligatoria CSV: `full_date`. Le altre colonne opzionali. Gestire **BOM UTF-8** (`utf-8-sig`) e header mancanti con errore chiaro.
  - **D8.c** La colonna `scales` CSV (formato `Nome: Livello k/N`) va memorizzata sia come **pedice ordinato** (int) sia come **etichetta** (string). Le scale possono differire da utente a utente (utenti "premium").
  - **D8.d** `activities` esposte come lista (stringa separata da ` | ` normalizzata → lista).
- **D9 — Documentazione e automazione flatpress:** Gitea Actions (`.gitea/workflows/ci.yml`) con gate `[skip-ci]`, lint ruff, pytest, package/release opzionale, bump automatico `version.ini`, notifica Ntfy, README inglese, WORKPLAN italiano, wiki (fallback `docs/`), commenti MAINTENANCE NOTES, logging a livelli configurabile via env `LOG_LEVEL`.
- **D10 — API parametrica (disegno):** endpoint REST per l'elenco utenti, per i record (filtri/date/colonne incluse), per le config di colonne/scale, per la selezione colonne arbitrarie, e per l'upload dei file. La dashboard usa endpoint JSON dedicati che leggono **solo Influx**.
- **D11 — Assets `.daylio`: default IGNORATE.** Gli array `assets` (foto/media) non vengono importati né esposti nella prima versione; il criterio è estensibile (flag/config per abilitare in futuro; storage dedicato non previsto adesso).
- **D12 — Lingua documentazione:** README e documentazione utente in inglese; WORKPLAN e docs di lavoro in italiano. Documentazione da approvare **prima** della scrittura del codice: nessuna modifica applicativa prima dell'approvazione di questo WORKPLAN.

---

- **D13 — Struttura repository (NOVITÀ, Integrazione 1).** Il progetto viene riorganizzato con sorgenti Python isolati in `src/daylio_api/` (package), sorgenti non-Python separati nelle proprie cartelle, test separati unit/integration, config YAML in `config/`, dashboard Chart.js vendored in `dashboard/static/vendor`, sample-data dedicati. Albero target completo in §5.1. **Motivazione "lean"**:
  - **`src/` layout**: import puliti (`src` nel `sys.path` via `pyproject.toml`/editable install), niente root-path hack, package esplicito, test che importano il package reale.
  - **Test separati `unit/` + `integration/`**: veloci e isolati vs. integrazioni reali (import→SQLite→Influx, API, scheduler); `conftest.py` centralizza fixture; `tests/fixtures/` usa **sample-data reali ridotti**.
  - **Config YAML separata dai segreti**: `config/config.example.yml` + `config/users.example.yml`; i segreti (token Influx, admin password, credenziali Docker) solo in env/secret → niente segreti nel repo.
  - **Dashboard vendored**: `dashboard/static/vendor/` (Chart.js), niente CDN di terze parti in produzione (offline-safe, no data leak).
  - **Scripts separati** (`scripts/`): utilità dev/ops non importate dal runtime (seed admin, dump, import CLI).
  - **Dockerfile multi-stage**: build Python separata dal runtime → immagine finale più piccola e lean.

---

- **D14 — Workflow CI completo: job build&push Docker multi-arch (NOVITÀ, Integrazione 2, CONFERMATO 2026-09-22).** Il `.gitea/workflows/ci.yml` combina **FLUSSO FLATPRESS** (gate `[skip-ci]`, lint ruff, pytest, release su `workflow_dispatch` — **sempre manuale, MAI su tag**, bump `version.ini`, notify) con **FLUSSO INFRASTRUCTURE-TOOLS** (job `build` **solo su push**, guardia `[skip]` sul commit, container `alpine:3.23`, `strategy.matrix` con `fail-fast:false`, buildx multi-arch `linux/amd64,linux/arm64` con `--push`, login docker via stdin, guardia CA interna `DUNGEONCACERT`, checkout `fetch-depth:0`). Schema dei job in §8.2.
- **D15 — Immagine Docker pubblicata (CONFERMATO 2026-09-22).**
  - **D15.a** Immagine: `daylio-api-server` su registro `docker.io` (**default confermato** `tanadelgigante/daylio-api-server`, sovrascrivibile con `vars.IMAGE_REPO`).
  - **D15.b Tag**: derivato da `version.ini` → `v<version>` (es. `v2.0.0`), fallback `vars.APP_VERSION`, fallback `matrix.tag`/`github.run_number`. **CONFERMATO**.
- **D16 — Notifica unica Ntfy** sul pattern infrastructure-tools (job `notify`, `if: always()`, `needs: [build]`, guardia su `NTFY_URL`/`NTFY_TOPIC`, mapping success/failure/cancelled). (Complementare al notify flatpress multi-job.) **CONFERMATO 2026-09-22**.

---

- **D17 — Config applicativa in YAML (NOVITÀ, richiesta utente 2026-09-22).** La configurazione passa da TOML/tomllib a **YAML** con **PyYAML** (dipendenza minima). File: `config/config.example.yml` (base, completo e commentato in inglese per R12) + `config/users.example.yml` (overrides per utente). I segreti restano solo in env/secret (`env:INFLUX_TOKEN`, ecc.). Schema documentato in §6.
- **D18 — InfluxDB: org/bucket e endpoint per CI (CONFERMATO 2026-09-22).** Org **`tanadelgigante`**, bucket **`daylio`**. Il servizio InfluxDB reale è su **192.168.188.120**. In CI il job `test` costruisce l'URL così: `INFLUX_URL="http://${INFLUX_INSTANCE}"` dove `INFLUX_INSTANCE` è una **vars di repo Gitea** (`<ip>:<port>`); org/bucket/token da env/secret. Aggiorna §6 (env table), §8.2/§8.3, §8.4, §5.4.

---

## 5. Architettura target

### 5.1 Struttura repository target (D13)

```
daylio-api-server/
├── src/
│   └── daylio_api/
│       ├── __init__.py
│       ├── app.py                  # app factory Flask (crea app, registra blueprint, config, logger)
│       ├── config.py               # caricamento config YAML (PyYAML) + env; merge default/utente
│       ├── logger.py               # logger a livelli (DEBUG/INFO/WARN/ERROR/FATAL) configurabile via env
│       ├── db/
│       │   ├── __init__.py
│       │   ├── sqlite_store.py     # accesso SQLite (utenti, config, record giornalieri, audit) + migrazioni
│       │   ├── migrations.py       # migrazioni versionate (CREATE/ALTER idempotenti)
│       │   └── influx_store.py     # scrittura/overwrite + lettura InfluxDB (analytics/dashboard)
│       ├── importers/
│       │   ├── __init__.py
│       │   ├── base.py             # interfaccia importer + normalizzazione condivisa
│       │   ├── csv_importer.py     # parser CSV nativo (csv stdlib, utf-8-sig, BOM), colonne opzionali
│       │   ├── daylio_importer.py  # ZIP -> backup.daylio (Base64) -> JSON; month 0-based; customMoods; tags; scales; scaleValues
│       │   └── normalizer.py       # mapping mood 1-5, scala salute da tags, activities, scales, dedup giornaliero "vince la più recente"
│       ├── api/
│       │   ├── __init__.py
│       │   ├── routes.py           # endpoint REST /api/* (parametrici, filtri, selezione colonne)
│       │   ├── auth.py             # login, sessioni firmate (itsdangerous)/API key, admin-only user creation
│       │   └── serializers.py      # serializzazione record + parametri colonne
│       ├── dashboard/
│       │   ├── __init__.py
│       │   ├── views.py            # route /dashboard e endpoint JSON /api/analytics/* (solo Influx)
│       │   └── static/
│       │       ├── index.html
│       │       ├── app.js
│       │       └── vendor/         # Chart.js (vendored, no CDN di terze parti in produzione)
│       ├── scheduler.py            # job periodici APScheduler per utente (intervallo + ora inizio)
│       └── audit.py                # import_jobs (id, file, conteggi importati/aggiornati/saltati/errori)
├── config/
│   ├── config.example.yml          # config base di esempio: colonne, scale (mood, Stress, Scala salute), scheduler, influx, logging (YAML)
│   └── users.example.yml           # esempio overrides per utente (directory di import, scale custom, scheduler)
├── tests/
│   ├── __init__.py
│   ├── conftest.py                 # fixture: tmp SQLite, mock Influx (o testcontainer), fixture sample-data
│   ├── fixtures/                   # campioni reali ridotti da sample-data (CSV e .daylio)
│   ├── unit/                       # test unitari (normalizer, parser, config, logger, auth)
│   └── integration/                # test integrazione (import->SQLite->Influx, API, scheduler)
├── sample-data/                    # campioni reali già presenti (daylio_export_*.csv, backup_*.daylio)
├── docs/                           # WORKPLAN.md + documentazione tecnica (es. REPORT/ADR)
├── scripts/                        # utilità dev/ops non importate dal runtime (es. seed admin, dump, import CLI)
├── Dockerfile                      # immagine dell'app (multi-stage: build + runtime)
├── docker-compose.yml              # app + influxdb (dalla composizione utente: influxdb:latest, porta 8120:8086, volumi /var/influx/...)
├── .dockerignore
├── .gitea/workflows/ci.yml         # workflow completo (vedi §8)
├── pyproject.toml                  # metadata, dipendenze, tooling (ruff, pytest)
├── requirements.txt / requirements.lock  # dipendenze pinnate/hash (pip-tools)
├── version.ini                     # es. 2.0.0-SNAPSHOT (bump automatico in release)
├── .gitignore
├── README.md                       # in inglese
└── LICENSE
```

> Nota: il modulo migrazioni è **rinominato `db/migrations.py`** (decisione confermata 2026-09-22, sostituisce il nome `db/ingestora_migrations.py` del layout iniziale).

### 5.2 Moduli del package `src/daylio_api/`

- **`app.py`** — app factory `create_app(config_file, env)`; registra blueprint `api` e `dashboard`; configura logger e store; espone `wsgi` app per Waitress.
- **`config.py`** — carica `config/config.example.yml` (e il file reale di deploy, stesso schema) via **PyYAML**, applica overrides per utente (sezione `users.<username>` o file dedicato in `config/`), merge con env (`INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_BUCKET`, `INFLUX_INSTANCE`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `LOG_LEVEL`, `IMAGE_REPO`, `APP_VERSION`, ecc.). Validazione schema config.
- **`logger.py`** — `get_logger(name)` con livelli **DEBUG/INFO/WARN/ERROR/FATAL**, soglia da `LOG_LEVEL` (default `INFO`); WARN/ERROR/FATAL sempre attivi; formato `[daylio_api] <LEVEL> <msg> [ctx k=v]` (analogo a `PublishArticleLogger.php`). Testabilità con capture.
- **`db/sqlite_store.py`** — connessione SQLite (path configurabile), accesso a utenti, config, record giornalieri, audit; esegue migrazioni idempotenti.
- **`db/migrations.py`** — migrazioni versionate (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE` con guardie), `schema_version` in `pragma user_version` o tabella dedicata.
- **`db/influx_store.py`** — client `influxdb-client`; scrittura (point giornaliero), **overwrite completo** (D3: stesso measurement+tags+time a inizio/mezzogiorno del giorno + gestione campi non più presenti: write di valori vuoti/default o delete del vecchio punto prima del write), lettura per analytics/dashboard.
- **`importers/base.py`** — interfaccia `ImportResult`; normalizzazione condivisa (mood, activities, scales, scala salute da tags, dedup "vince la più recente").
- **`importers/csv_importer.py`** — parser CSV stdlib; `utf-8-sig` (BOM); header atteso `full_date,...`; colonne opzionali eccetto `full_date` (D8.b); gestione duplicati con regola più recente; estrazione `scales` (`Nome: Livello k/N`) e scala salute da `activities` secondo config (D2, D8.c).
- **`importers/daylio_importer.py`** — magic bytes ZIP (PK) → `backup.daylio` → Base64 → JSON (version 15); mappa customMoods (1=best…5=worst), tags+tag_groups (scala salute da id_tag_group=5), scales (text_scale/number_scale), dayEntries (month 0-based, FK mood, tags, scaleValues, assets→D11 ignorate).
- **`importers/normalizer.py`** — produce il **record normalizzato** finale (dict canonico): `full_date, date/weeday derivati, time, mood_num 1-5, mood_label, activities list, scales dict {name: {value:int,label:str}}` + scala salute, `note_title`, `note`. Applica `vince la più recente` (D3).
- **`api/routes.py`** — blueprint `/api/*`: `GET /api/users`, `GET /api/records`, `GET /api/columns`, `GET /api/scales`, `POST /api/import` (upload), `POST /api/import/folder` (trigger cartella), ecc. Parametrici: `columns`, `start_date`, `end_date`, `user`.
- **`api/auth.py`** — PBKDF2 (Werkzeug), sessioni signed (itsdangerous) e/o API key per script; decorator `@admin_required`; creazione utenti admin-only.
- **`api/serializers.py`** — mapping record SQLite → JSON con selezione colonne.
- **`dashboard/views.py`** — route `GET /dashboard` (serve SPA) e endpoint JSON `GET /api/analytics/*` che leggono **SOLO da Influx** (D4/R4).
- **`dashboard/static/`** — `index.html`, `app.js`, `vendor/` (**Chart.js** vendored, confermato 2026-09-22).
- **`scheduler.py`** — job periodici **APScheduler** (non threading nativo) per import automatico per utente (intervallo + ora inizio), riusa `import_data(user, source=…)`.
- **`audit.py`** — scrittura righe `import_jobs`: id, user, file/source, conteggi (imported/updated/skipped/errors), timestamp, esito.

### 5.3 Modello dati SQLite

Tabelle principali (schema v1, migrazioni idempotenti):

- **`users`** — `id INTEGER PK`, `username TEXT UNIQUE`, `password_hash TEXT`, `is_admin INTEGER DEFAULT 0`, `import_dir TEXT` (directory configurabile, nullable), `created_at`, `updated_at`.
- **`columns_config`** — `id`, `user_id FK` (nullable = default), `column_name TEXT` (es. `mood`, `activities`, `scales`, `note`, `note_title`), `enabled INTEGER`, `source TEXT` (csv/daylio), `options_json` (es. mapping).
- **`scales_config`** — `id`, `user_id FK` (nullable = default), `scale_name TEXT` (es. `mood`, `stress`, `scala_salute`), `scale_type TEXT` (`text`/`number`), `values_json` (es. `{"alla grande":1,…}` o `{"ottimo":1,…, "terribile":8}`), `min`/`max` per number, `source` (colonna CSV vs tag group).
- **`mood_map`** — migrazione della vecchia tabella: `mood_text TEXT PK`, `mood_value INTEGER` (1-5 D1). Seeded dal default, aggiornabile via config.
- **`day_entries`** — `id INTEGER PK`, `user TEXT FK users.username`, `full_date TEXT`, `time TEXT`, `mood INTEGER` (1-5), `mood_label TEXT`, `activities TEXT` (JSON array o delimitato), `scales TEXT` (JSON: `{"Stress": {"value":3,"label":"Moderato"}, "Scala salute": {"value":…, "label":…}}`), `note_title TEXT`, `note TEXT`, `source TEXT` (csv/daylio), `created_at`, `updated_at`, **`UNIQUE(user, full_date)`** → vince R5/D3. Upsert `ON CONFLICT(user, full_date) DO UPDATE`.
- **`import_jobs`** (audit) — `id INTEGER PK`, `user TEXT`, `source TEXT` (upload/cartella/daily), `filename TEXT`, `imported INTEGER`, `updated INTEGER`, `skipped INTEGER`, `errors INTEGER`, `detail TEXT` (JSON errori), `started_at`, `finished_at`, `status TEXT`.
- **`schema_version`** — `version INTEGER`.

### 5.4 Modello InfluxDB

- **Bucket**: `daylio` (confermato 2026-09-22). **Org**: `tanadelgigante` (confermato; via `INFLUX_ORG`). Token via env.
- **Servizio InfluxDB reale**: `192.168.188.120` (porta 8120 pubblicata sul servizio docker-compose). In CI/test l'URL è costruito da `vars.INFLUX_INSTANCE` (`<ip>:<port>`) → `INFLUX_URL="http://${INFLUX_INSTANCE}"` (D18).
- **Measurement**: `daylio_entry` (proposta; sovrascrivibile in config).
- **Tags**: `user` (username), `date` (`YYYY-MM-DD`), opzionali `mood_label`, `scala_salute_label`.
- **Fields** (numerici per analytics): `mood` (1–5), `mood_num` (alias), conteggi `activities_count`, per ogni scala custom `scale_<name>_value` (es. `scale_stress_value`, `scale_scala_salute_value`), `is_favorite` (0/1), `notes_present` (0/1).
- **Fields** string (per detail): `activities` (JSON array), `scales` (JSON), `note_title`, `note` (opzionale), `source`.
- **Timestamp**: **giornaliero** — punto scritto con timestamp **a inizio giornata** (`YYYY-MM-DDT00:00:00Z`) o a mezzogiorno (decisione D3/§5.4) per garantire **overwrite completo** dell'intero punto del giorno all'upsert. Strategia di update consigliata: **delete-and-write** del punto del giorno `(user, date)` prima del nuovo write, oppure scrittura con timestamp fisso diurno (overwrite per default del TSDB). Gestire esplicitamente i **campi non più presenti** (write di valori vuoti/default o delete del vecchio punto) per garantire update completo (vedi rischio R8).
- L'utente fornisce docker-compose InfluxDB 2.x: `image influxdb:latest`, porta `8120:8086`, volumi `/var/influx/influxdb:/var/lib/influxdb2` e `/var/influx/config:/etc/influxdb`, `restart: always` → da includere nel `docker-compose.yml` del progetto (servizio `influxdb`) per dev/test.

### 5.5 Flusso import unificato

1. **Rilevamento formato**: leggere magic bytes (CSV testo vs ZIP `PK\003\004`).
2. **Parsing** tramite importer dedicato (CSV stdlib con BOM / `.daylio` ZIP+JSON).
3. **Normalizzazione** (normalizer.py): mood 1-5, activities lista, scales (`Nome: Livello k/N` → value+label), scala salute da tags/activities, note.
4. **Dedup giornaliero**: "vince la più recente" per `(user, full_date)` (per `time` decrescente, poi per `datetime`/id).
5. **Validazione scale**: solo scale/etichette presenti nella config (errori chiari con riga/file).
6. **Upsert SQLite** (`ON CONFLICT(user, full_date) DO UPDATE`).
7. **Upsert Influx** (punto del giorno, overwrite completo) dopo commit SQLite OK.
8. **Audit** (`import_jobs`) con conteggi imported/updated/skipped/errors.

### 5.6 API REST parametrica (bozza endpoint)

| Endpoint | Metodo | Descrizione |
| --- | --- | --- |
| `/api/users` | GET | Elenco utenti (admin o limitato all'utente autenticato) |
| `/api/records` | GET | Record giornalieri con filtri `user`, `start_date`, `end_date`, `columns` (selezione colonne), `page`/`limit`; sorgente SQLite (fonte verità) |
| `/api/records/<full_date>` | GET | Singolo record del giorno |
| `/api/columns` | GET | Colonne/schema configurabili (da `columns_config`+`scales_config`) |
| `/api/scales` | GET | Scale configurate (mood, Stress, Scala salute, custom per utente) |
| `/api/import` | POST | Upload file (CSV/.daylio) — form multipart |
| `/api/import/folder` | POST | Trigger import da cartella utente |
| `/api/import/jobs` | GET | Audit/import_jobs |
| `/api/analytics/*` | GET | Endpoint JSON per dashboard — **solo Influx** (D4/R4) |
| `/dashboard` | GET | SPA dashboard (servita static + chart) |
| `/auth/login`, `/auth/logout` | POST/GET | Auth utente |
| `/admin/users` | POST/GET | Creazione/lista utenti (admin-only) |

### 5.7 Dashboard (R4)

- SPA leggera servita da Flask (`src/daylio_api/dashboard/static/`), grafici con **Chart.js vendored** (confermato 2026-09-22).
- Dati solo via `/api/analytics/*` che interroga **esclusivamente InfluxDB** (query Flux aggregate: mood nel tempo, distribuzione mood, heatmap giorno/settimana, trend scala salute/Stress, conteggi activities).
- Filtri per utente e intervallo date; autenticazione come per API.

---

## 6. Configurazione

> **Formato: YAML** (PyYAML) — richiesta esplicita dell'utente 2026-09-22 (**D17**). Il TOML/tomllib usato nelle versioni precedenti del piano è abbandonato.

**File**:
- `config/config.example.yml` — config base **di esempio**, completa e commentata (commenti in inglese per R12). È il riferimento dello schema.
- `config/users.example.yml` — esempio overrides per utente (directory di import, scale custom per utenti "premium", scheduler override).
- In produzione: il deploy copia `config.example.yml` in `config/config.yml` (o equivalente) e adatta i valori; `users.example.yml` viene replicato per utente reale. I **segreti** (token Influx, admin password) restano **solo in env/secret**, mai nei file YAML.

**Schema `config.example.yml` (esempio):**
```yaml
app:
  log_level: "INFO"               # DEBUG/INFO (WARN/ERROR/FATAL sempre attivi)
  timezone: "Europe/Rome"

storage:
  sqlite_path: "data/daylio.db"
  influx_url: "env:INFLUX_URL"    # in CI: http://${INFLUX_INSTANCE}; reale: http://192.168.188.120:8120
  influx_org: "tanadelgigante"    # CONFERMATO 2026-09-22
  influx_bucket: "daylio"
  influx_token: "env:INFLUX_TOKEN"  # segreto da env

mood:
  # mapping etichetta -> pedice ordinato (1 = migliore, 5 = peggiore) — D1
  values:
    "alla grande": 1
    "bene": 2
    "così così": 3
    "male": 4
    "malissimo": 5

scales:
  stress:
    type: "text"                  # 'text' | 'number'
    source: "column"              # colonna CSV `scales` formato "Stress: Livello k/N"
    values: ["Calma", "Basso", "Moderato", "Alto", "Sopraffatto"]
  scala_salute:
    type: "text"
    source: "tag-group:5"         # nel .daylio: tags id_tag_group=5; nel CSV: dentro activities
    values:
      - "ottimo"
      - "buono"
      - "discreto"
      - "sufficiente"
      - "mediocre"
      - "insufficiente"
      - "pessimo"
      - "terribile"

columns:
  required: ["full_date"]
  optional: ["date", "weekday", "time", "mood", "activities", "scales", "note_title", "note"]
  derive: ["date", "weekday"]     # derivati da full_date (default)

scheduler:
  backend: "apscheduler"          # APScheduler (confermato 2026-09-22), non threading nativo
  interval_seconds: 86400         # default: ogni 24h
  start_hour: 0                   # ora di inizio della scansione
  per_user:                       # override per utente
    alice: { interval_seconds: 3600, start_hour: 6 }

auth:
  session_ttl: 86400              # sessioni firmate itsdangerous per Web UI
  api_keys:
    enabled: true                 # API key per script automatici

logging:
  level: "INFO"
```

**Env var principali (segreti e run-time):**
| Env | Scopo |
| --- | --- |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Credenziali admin (hash PBKDF2 salvato al primo avvio) |
| `INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG` (=`tanadelgigante`), `INFLUX_BUCKET` (=`daylio`) | Accesso InfluxDB |
| `INFLUX_INSTANCE` | **CI/test** piuttosto che URL completo: `vars.INFLUX_INSTANCE="<ip>:<port>"` (es. `192.168.188.120:8120`) → il job `test` costruisce `INFLUX_URL="http://${INFLUX_INSTANCE}"` (D18) |
| `LOG_LEVEL` | Soglia logging (`DEBUG`/`INFO`; WARN/ERROR/FATAL sempre attivi) |
| `DATA_DIR` / `DB_PATH` | Path data/db (default `data/`, `data/daylio.db`) |
| `DOCKER_REGISTRY`, `DOCKER_USERNAME`, `DOCKER_PASSWORD` | Credenziali registry per CI build (solo CI, non app) |
| `IMAGE_REPO`, `APP_VERSION` | Nome immagine e tag per CI (vars) |
| `NTFY_URL`, `NTFY_TOPIC` | Notifiche Ntfy (vars) |

---

## 7. Fasi di lavoro (0..7)

> Responsabili: **Coder** (C), **Tester** (T), **Writer** (W). Ogni fase ha deliverable e criteri di accettazione. Le fasi sono sequenziali salvo dove indicato.

### FASE 0 — Baseline, housekeeping e struttura repository (C, T) — AGGIORNATA con D13
**Obiettivo:** partire da worktree pulito, struttura `src/` pronta, dipendenze lean, test infra in piedi. **In questa fase si applica D13** (nuovo layout) e si corregge il bug mood (D1).

- **Task 0.1 — Housekeeping git** (C): rimuovere `data.db` dai tracciati (`git rm --cached data.db`, aggiungere `data.db`, `data/`, `.env`, `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/` al `.gitignore`; sostituire `sampledata/*` con `sample-data/`); commit.
- **Task 0.2 — Creazione struttura repository** (C): applicare **albero target §5.1** (D13): `src/daylio_api/…`, `config/`, `tests/`, `scripts/`, `docs/`, `.gitea/workflows/`, `pyproject.toml`, `version.ini`, `.dockerignore`.
- **Task 0.3 — Dipendenze lean** (C): rimuovere pandas; `requirements.txt`/`requirements.lock` con `flask>=3.0`, `waitress`, `influxdb-client`, `itsdangerous`, `werkzeug`, `pyyaml` (config YAML), `apscheduler`; dev: `pytest`, `ruff`, `pip-tools`; pin + hash; `pyproject.toml` con tooling.
- **Task 0.4 — Config base e logging** (C): `config/config.example.yml` + `config/users.example.yml` (schema §6, YAML), `logger.py` a livelli (D9), env `LOG_LEVEL`.
- **Task 0.5 — Fixture test** (T): `tests/conftest.py` (sqlite tmp, mock Influx, fixture sample-data); `tests/fixtures/` con CSV/.daylio reali ridotti.
- **Task 0.6 — Dockerfile multi-stage e docker-compose Aggiornati** (C): `Dockerfile` (build Python 3.12 + runtime Waitress), `.dockerignore`, `docker-compose.yml` con app + servizio `influxdb` (porta `8120:8086`).

**Deliverable Fase 0:** struttura `src/` pronta, git pulito, dipendenze pinnate, fixture test, logger, Dockerfile multi-stage, WORKPLAN aggiornato committato.
**Criteri di accettazione Fase 0:** `pytest` verde (test base/conftest), `ruff check .` senza errori, `python -c "import daylio_api"` OK, immagine docker build ok; `git status` senza `data.db`.

### FASE 1 — Configurazione colonne/scale (R1) (C, T)
**Obiettivo:** sistema configurabile per colonne e scale (D1, D2, D8), persistito in SQLite e YAML.
- Task 1.1 — Modello SQLite: `columns_config`, `scales_config`, `mood_map` (migrazioni idempotenti).
- Task 1.2 — `config.py`: caricamento YAML (PyYAML) + env + merge overrides per utente; validazione schema.
- Task 1.3 — API di lettura config (`/api/columns`, `/api/scales`).
- Task 1.4 — Seeder default (mood 1-5, Stress, Scala salute).

**Deliverable:** modulo config + tabelle + endpoint lettura.
**Criteri di accettazione:** config valida per i sample-data; override per utente verificato con test; valori default coerenti con D1/D2.

### FASE 2 — Import unificato CSV + .daylio + normalizzazione (R8) (C, T)
**Obiettivo:** parser e normalizzatore unificati (D7, D8).
- Task 2.1 — `csv_importer.py`: `utf-8-sig`, header richiesti, colonne opzionali, gestione duplicati.
- Task 2.2 — `daylio_importer.py`: ZIP→JSON, customMoods, tags/tag_groups, scales, dayEntries, **month 0-based**, scaleValues, assets ignorate (D11).
- Task 2.3 — `normalizer.py`: mood 1-5, activities lista, scales value+label, scala salute da tags/activities, "vince la più recente".
- Task 2.4 — Test con sample reali (630 righe): mood count atteso, 58 scale Stress, scala salute estratta, **2 giorni duplicati normalizzati a 1 record ciascuno**.

**Deliverable:** importatori + normalizzatore + test.
**Criteri di accettazione:** test su `daylio_export_2026_09_22.csv` e `backup_2026_09_22.daylio` verdi; normalizzazione coincidente tra CSV e .daylio per le stesse date.

### FASE 3 — Unicità/upsert e store SQLite + Influx + audit (R3, R5) (C, T)
**Obiettivo:** chiave `(user, full_date)` e scrittura coordinata sui due storage (D3, D4).
- Task 3.1 — `sqlite_store.py`: schema `day_entries` con `UNIQUE(user, full_date)`, migrazione da vecchia tabella, upsert `DO UPDATE`.
- Task 3.2 — `influx_store.py`: scrittura punto giornaliero (overwrite completo D3/§5.4), lettura analytics.
- Task 3.3 — `audit.py` + tabella `import_jobs`.
- Task 3.4 — Strategia coordinate: SQLite commit → Influx write; job di **ri-sincronizzazione** Influx da SQLite (resync) manuale/periodico.
- Task 3.5 — Test integrazione: import dei sample → verifica 1 record/giorno (duplicati 2025-01-01/03 → più recente), upsert dopo modifica passata, coerenza SQLite↔Influx.

**Deliverable:** store SQLite+Influx coordinati, audit, resync.
**Criteri di accettazione:** upsert produce 1 riga/giorno anche con date duplicate; modifica giorno passato aggiorna entrambi gli store; audit popolato.

### FASE 4 — API REST parametrica (R2) (C, T)
**Obiettivo:** API parametrica su tutte le colonne (D10).
- Task 4.1 — `api/routes.py` + `serializers.py`: `/api/records` con `columns`, filtri data, paginazione.
- Task 4.2 — Ritorna anche scales normalizzate e scala salute.
- Task 4.3 — Error handling uniforme, validazione parametri, compatibilità con endpoint legacy (`/users`, `/moods`).
- Task 4.4 — Test API (includendo selezione colonne e filtri).

**Deliverable:** API parametrica documentata.
**Criteri di accettazione:** test API verdi; endpoint legacy ancora disponibili (retrocompat).

### FASE 5 — Auth multiutente + cartella import + scheduler (R6, R7) (C, T)
**Obiettivo:** utenti admin-only, isolamento, import upload+cartella periodico.
- Task 5.1 — `api/auth.py`: hash PBKDF2, sessioni (itsdangerous) e/o API key, `@admin_required`.
- Task 5.2 — Gestione utenti admin-only (`/admin/users`); isolamento dati per utente su SQLite/Influx (filtri `user`).
- Task 5.3 — Upload file CSV/.daylio via Web UI/API (`POST /api/import`).
- Task 5.4 — Cartella di import configurabile per utente (`import_dir`) + `scheduler.py` con **APScheduler** (intervallo, ora inizio) per-utente.
- Task 5.5 — Test auth (403 admin, sessioni, API key), test upload, test scheduler (mock clock/fixture).

**Deliverable:** multiutenza + import upload/folder + scheduler.
**Criteri di accettazione:** solo admin crea utenti; utente vede solo i propri dati; upload e import da cartella funzionano; scheduler invocabile/configurabile.

### FASE 6 — Dashboard integrata su InfluxDB (R4) (C, T, W)
**Obiettivo:** dashboard SPA che legge SOLO da InfluxDB (D4/R4).
- Task 6.1 — `dashboard/views.py` + endpoint `/api/analytics/*` (query Flux, solo Influx).
- Task 6.2 — Frontend `static/index.html`, `app.js`, `vendor/` (Chart.js vendored).
- Task 6.3 — Auth + filtro utente/date; test frontend via API endpoint mockati (pytest vs testcontainer Influx se disponibile).

**Deliverable:** dashboard funzionante.
**Criteri di accettazione:** dashboard carica dati esclusivamente da Influx (nessun fallback/read su SQLite per analytics); test endpoint analytics.

### FASE 7 — Automazione CI, release, documentatione (R9, R12) (C, T, W) — AGGIORNATA con Integrazione 2
**Obiettivo:** workflow completo (flatpress + infrastructure-tools), logging, docs, wiki.

- **Task 7.1 — Workflow `.gitea/workflows/ci.yml` completo** (C): job `ci-skip-check` (gate `[skip-ci]` flatpress), `lint` (ruff), `test` (pytest), **`build` (Docker multi-arch push, pattern infrastructure-tools)**, `release` (opzionale/workflow_dispatch flatpress), `notify` (Ntfy). Schema completo in §8.
- **Task 7.2 — Logging a livelli nel codice**: DEBUG/INFO configurabili via `LOG_LEVEL`, WARN/ERROR/FATAL sempre attivi; commenti "MAINTENANCE NOTES" in inglese nel codice.
- **Task 7.3 — `version.ini`** + bump automatico post-release (flatpress pattern).
- **Task 7.4 — README inglese** + docs in `docs/` + **Wiki Gitea** (fallback `docs/` con indice incrociato).
- **Task 7.5 — `.dockerignore`** e build test dell'immagine nel job `build`.
- **Task 7.6 — Matrice di test CI eseguita** (§8.4).

**Deliverable:** CI completo (lint/test/build push/notify/release opzionale), docs/wiki, logging.
**Criteri di accettazione:** run CI verde; push dell'immagine `daylio-api-server` multi-arch avvenuto su registry; notifica Ntfy ricevuta; `[skip-ci]`/`[skip]` rispettati; README inglese e WORKPLAN italiano presenti; bump version.ini automatico.

---

## 8. Workflow CI (`.gitea/workflows/ci.yml`) — schema job e matrice

### 8.1 Origine dei pattern
| Pattern | Da | Usato per |
| --- | --- | --- |
| Gate `[skip-ci]`, lint ruff, pytest, package/release su `workflow_dispatch`, bump `version.ini`, logging | `flatpress-publish-article-plugin/.gitea/workflows/ci.yml` | Job `ci-skip-check`, `lint`, `test`, `release` |
| Trigger `on: [push, pull_request]`, guardia `[skip]`, runner `local`, container `alpine:3.23` con `--dns`/`--add-host`, `strategy.matrix` fail-fast:false, install deps + CA (`apk_install_with_retry`, QEMU, `DUNGEONCACERT`), Setup Docker buildx, checkout `fetch-depth:0`, **Build and push** multi-arch (`git diff` guard, tag, login stdin, `--platform`+`--push`), job `notify` Ntfy | `infrastructure-tools/.gitea/workflows/ci.yml` | Job `build` + `notify` |

### 8.2 Job del workflow (schema combinato)
> Trigger: `push`, `pull_request`, `workflow_dispatch` (per release). Runner: `local` (act_runner). Container dei job: `alpine:3.23` per lint/test/build (con toolchain aggiuntive) oppure `python:3.12-slim` per i job Python (scelta Coder — vedi Nota). Chiave `strategy.matrix` con `fail-fast: false` e `include` per l'immagine (`daylio-api-server`). **Decisioni confermate 2026-09-22**: `build` solo su push; release **sempre manuale** via `workflow_dispatch`.

| # | Job | Dipende da | Guardia | Descrizione |
| --- | --- | --- | --- | --- |
| 0 | `ci-skip-check` | — | — | Calcola `should_skip`: `[skip-ci]` nel commit/PR (flatpress pattern); `workflow_dispatch` mai skippato |
| 1 | `lint` | `ci-skip-check` | `should_skip != 'true'` | `ruff check .` (container python) |
| 2 | `test` | `ci-skip-check` | `should_skip != 'true'` | `pytest` (container python, fixture sample reali). **Per i test di integrazione Influx**: costruisce `INFLUX_URL="http://${INFLUX_INSTANCE}"` dove `INFLUX_INSTANCE` è `vars.INFLUX_INSTANCE` (`<ip>:<port>`, es. `192.168.188.120:8120`); org/bucket/token da env/secret (D18). Se `INFLUX_INSTANCE` non impostata, i test integrazione Influx vengono skippati (solo unit) |
| 3 | `build` | `ci-skip-check` | `should_skip != 'true'` + guardia `[skip]`/`[skip-ci]` sul commit + **diff-guard Dockerfile** + **solo `github.event_name == 'push'`** (confermato) | **Build&push immagine Docker multi-arch** (pattern infrastructure-tools §8.3) |
| 4 | `release` (manuale) | `lint, test, ci-skip-check` | **SOLO `workflow_dispatch job=release`** (confermato: mai automatica su push/tag) | Crea release Gitea + bump `version.ini` (flatpress pattern) |
| 5 | `notify` | tutti (`always()`) | — | Notifica Ntfy unica (pattern infrastructure-tools+flatpress) |

**Nota sui container di job**: nel flusso infrastructure-tools i job girano in `alpine:3.23` con toolchain installata via `apk_install_with_retry`; nel nostro workflow i job `lint`/`test` (Python) possono usare un container `python:3.12-slim` (più lean per pip/pytest/ruff), mentre `build` (Docker buildx multi-arch) usa `alpine:3.23` con `docker-cli`, `docker-cli-buildx`, `qemu`, `git`, `curl`, `ca-certificates` (come da riferimento). Il CI del nuovo progetto è da considerarsi **completo**: nessun job opzionale "tralasciato".

### 8.3 Dettaglio job `build` (adattato da infrastructure-tools)

Pseudo-flusso operativo (da implementare come step nel job build):

1. **Install dependencies & setup custom CA** (`alpine:3.23`):
   - funzione `apk_install_with_retry()` (max 5 tentativi, `sleep 5s`) per `git curl openssl-client docker-cli docker-cli-buildx qemu ca-certificates`;
   - `docker run --rm --privileged multiarch/qemu-user-static --reset -p yes || true` (registra binfmt per arm64);
   - CA interna: `DUNGEONCACERT` (secret) → `/usr/local/share/ca-certificates/dungeon-ca.crt` + `update-ca-certificates` (altrimenti warning).
2. **Setup Docker buildx**: `docker buildx create --name multiarch --driver docker-container --use` + `docker buildx inspect --bootstrap`.
3. **Checkout**: `actions/checkout@v4` con `fetch-depth: 0` (serve per diff-guard).
4. **Build and push**:
   - **Guardia idempotenza via diff**: `if git diff --name-only HEAD~1 HEAD 2>/dev/null | grep -q "^Dockerfile"; then` → build&push SOLO se il Dockerfile è cambiato nell'ultimo commit; altrimenti `"Dockerfile NON modificato, salto build."` (job passa comunque).
   - **Solo su push** (confermato 2026-09-22): il job `build` ha condizione `github.event_name == 'push'` — non gira su PR.
   - **Tag**: `version` da `version.ini` (strip `-SNAPSHOT`) → `TAG="v${version}"`; **fallback** `vars.APP_VERSION` → `v$APP_VERSION`; **ultimo fallback** `matrix.tag` (run_number). — *decisione D15b CONFERMATA 2026-09-22*
   - **Credenziali**: `if [ -z "$DOCKER_USERNAME" ] || [ -z "$DOCKER_PASSWORD" ]; then exit 1; fi`.
   - **Login**: `echo "$DOCKER_PASSWORD" | docker login --username "$DOCKER_USERNAME" --password-stdin "$DOCKER_REGISTRY"` (mai credenziali in argomento).
   - **Build multi-arch con push**: `docker buildx build --platform linux/amd64,linux/arm64 -t "$IMAGE:${TAG}" -f Dockerfile . --push` (con `${{ matrix.build_args }}` se presenti).
   - Output finale: `"✅ daylio-api-server:${TAG} pushed successfully (multi-arch)"`.

> **Dockerfile multi-stage**: il Dockerfile del progetto (già previsto in Fase 0/Task 0.6) deve essere multi-stage: stage di **build** (installa `pip-tools`, risolve `requirements.lock`, genera venv) + stage di **runtime** (immagine python slim, copia venv, CMD `waitress-serve`). Il job `build` costruisce questo Dockerfile dalla root del repo (`context: "."`).

### 8.4 Matrice di test CI

| # | Scenario | Job attesi | Note |
| --- | --- | --- | --- |
| 1 | Push su main di commit normale senza token | `ci-skip-check` false → `lint`, `test` eseguiti; `build` eseguito (solo push; se `Dockerfile` cambiato: push immagine; altrimenti skip build con job verde); `release` no; `notify` OK | Copre il caso di commit con modifiche al Dockerfile |
| 2 | Push su main di commit con `[skip-ci]` (o `[skip]` nel commit per il job `build`) | `ci-skip-check` → skip di lint/test/build/release; `notify` ne dà conto (skipped) | Verifica la convenzione token (flatpress/infrastructure) |
| 3 | `pull_request` | `ci-skip-check` (PR: titolo/commit head `[skip-ci]`) → `lint`+`test` eseguiti (o skip se token); **`build` MAI eseguito su PR** (confermato 2026-09-22) | Comportamento confermato: build solo su push |
| 4 | Push di tag `v*` | `lint`+`test` eseguiti (o skip); `build` eseguito se Dockerfile cambiato; **`release` MAI automatica su tag** (confermato 2026-09-22) | Release solo via `workflow_dispatch job=release` |
| 5 | `workflow_dispatch job=release` | Mai skippato; `release` crea release con version da `version.ini` | Flatpress pattern; **unica via di release** (manuale) |
| 6 | Dockerfile modificato nell'ultimo commit | `build` esegue buildx multi-arch + push | Diff-guard `HEAD~1..HEAD` |
| 7 | Dockerfile NON modificato | `build` esce con "NON modificato, salto build", job verde | Idempotenza |
| 8 | Job `test` con integrazione Influx | `test` costruisce `INFLUX_URL="http://${INFLUX_INSTANCE}"` da `vars.INFLUX_INSTANCE` (es. `192.168.188.120:8120`); org `tanadelgigante`, bucket `daylio`, token da secret; se `INFLUX_INSTANCE` non impostata → integrazione skippata, solo unit test | D18 — verifica su repo Gitea con vars/secret configurati |

---

## 9. Rischi e mitigazioni

| # | Rischio | Impatto | Mitigazione |
| --- | --- | --- | --- |
| R1 | Formato `.daylio` non documentato ufficialmente (evolve tra versioni; version 15 oggi) | Import fallito su struttura inattesa | Versionare l'estrattore; test su fixtures congelate; parse difensivo (campi mancanti); log WARN su campi ignoti |
| R2 | Ordine mood invertito (bug storico) | Dati storici interpretati al contrario | D1: migrazione del `mood_map` + job di normalizzazione storico; test di regressione mood |
| R3 | BOM UTF-8 / encoding CSV | Header/valori corrotti | `utf-8-sig`; test con sample reale BOM |
| R4 | Scale diverse per utenti (premium) | Config errata per utente | Overrides per utente in YAML; validazione; test multiutente |
| R5 | Perdita campi su update Influx (campi non più presenti) | Dashbboard incoerente con SQLite | Strategia overwrite delete+write (o campi default) D3/§5.4; test di upsert con modifica che rimuove scale |
| R6 | Granularità giornaliera vs intraday (più time nello stesso giorno) | Ambiguity su "quale entry vince" | Regola esplicita "vince la più recente per `time`, poi `datetime`/id"; documentata |
| R7 | Segreti in repo (token Influx, admin password) | Leak credenziali | Solo env/secret; `.gitignore`; configurazione esterna; CA interna via secret CI |
| R8 | Down/ritardo InfluxDB | Scrittura dashboard incompleta | Scrittura Influx dopo commit SQLite; retry/log ERROR; job di resync da SQLite; dashboard mostra eventuale "stale" |
| R9 | CSV con header mancanti/colonne rinominate | Import respinto o dato sbagliato | Validazione header con messaggi chiari; colonne opzionali (solo `full_date` obbligatoria); log DEBUG |
| R10 | Nome/registry/tag immagine non confermati | Push su registry sbagliato | D15/D15b **confermati 2026-09-22**; vars `IMAGE_REPO`/`APP_VERSION` |
| R11 | `.daylio` con month 0-based letto come 1-based | Date fuori di un mese | Normalizzazione esplicita `month+1`; test sui sample (2026-09-21 → month=8 vero) |
| R12 | Merge/duplicati da `.daylio` (630 entry = 630 CSV) | Doppio conteggio | Dedup giornaliero D3 in normalizer; test su entrambe le fonti |
| R13 | Dashboard legge solo Influx ma Influx vuoto | Dashboard vuota all'avvio | Job di import/resync iniziale; log; eventuale seed |
| R14 | CI "troppi job" (combinazione flatpress+infra) complessa | Manutenzione workflow | Schema job in §8.2 documentato; `[skip-ci]`; matrice di test §8.4 |
| R15 | Config YAML errata (sintassi/typo) | Avvio fallito o valori sbagliati | Validazione dello schema all'avvio (fail-fast con messaggi chiari); test su `config/config.example.yml`; commenti in inglese (R12) |
| R16 | `INFLUX_INSTANCE` non impostata in CI | Test integrazione Influx skippati o rotti | Skip condizionale dei test Influx-se `INFLUX_INSTANCE` assente (solo unit); vars/secret documentati in Gitea |

---

## 10. Definition of Done (generale)

1. Struttura repository target **D13** applicata (src/, config/, tests/, docs/, scripts/, sample-data/); git pulito (niente `data.db` tracciato).
2. **Decisioni D1–D18 applicate**: mood 1-5, scala salute 8 livelli (D2), unicità `(user, full_date)` + upsert (D3), doppio storage SQLite/Influx (D4), stack lean senza pandas (D5), multiutente admin-only + import folder + scheduler **APScheduler** (D6), formato `.daylio` supportato (D7), normalizzazione configurabile (D8), automazione flatpress (D9), API parametrica (R2), assets ignorate (D11), lingua/documentazione (D12), struttura `src/` (D13), CI completa con build/push (D14), immagine e tag confermati (D15), notify Ntfy (D16), **config YAML con PyYAML** (D17), **Influx org `tanadelgigante`/bucket `daylio`/`INFLUX_INSTANCE` per CI** (D18).
3. **Flusso import unificato** verificato sui sample reali: import CSV e `.daylio` producono gli stessi record; BOM gestito; 2 giorni duplicati normalizzati a 1 record (più recente); mood count attesi; 58 scale Stress estratte; scala salute estratta da activities/tags.
4. **Influx**: punti giornalieri scritti con overwrite completo; dashboard legge SOLO da Influx (R4); job di resync disponibile.
5. **API**: parametrica (selezione colonne + filtri), retrocompatibile con `/users` e `/moods` legacy.
6. **CI completa**: job `ci-skip-check`, `lint` (ruff), `test` (pytest, integrazione Influx via `vars.INFLUX_INSTANCE`), `build` (buildx multi-arch push `linux/amd64,linux/arm64` — **solo su push**), `release` (**solo manuale via `workflow_dispatch`, mai su tag**), `notify` (Ntfy); matrice di test §8.4 verde; push immagine `daylio-api-server` avvenuto (o skip documentato se Dockerfile invariato).
7. **Qualità**: pytest verde (failOnWarning-like), ruff senza errori, test con fixture dai sample reali; logging a livelli configurabile; commenti "MAINTENANCE NOTES" in inglese.
8. **Documentazione**: README inglese; WORKPLAN italiano presente e aggiornato; docs/ tecnici; **wiki Gitea prevista** (fallback docs/ con indice incrociato); `version.ini` con bump automatico.
9. **Approvazione**: questo WORKPLAN approvato dall'utente il **2026-09-22** (D12, tutti i punti del §11 risolti) prima della scrittura del codice applicativo.

---

## 11. FAQ / punti aperti per l'utente — **TUTTI RISOLTI 2026-09-22**

| # | Punto | Risposta utente | Stato |
| --- | --- | --- | --- |
| 1 | Nome immagine/registry (D15) | `tanadelgigante/daylio-api-server` su `docker.io`, override `vars.IMAGE_REPO` | **RISOLTO** |
| 2 | Derivazione tag (D15b) | `v<version.ini>` con fallback `vars.APP_VERSION` poi `matrix.tag` | **RISOLTO** |
| 3 | `build` su pull_request? | `build` **solo su push** | **RISOLTO** |
| 4 | Libreria dashboard | **Chart.js** (vendored) | **RISOLTO** |
| 5 | Auth | Sessioni itsdangerous per Web UI + API key per script | **RISOLTO** |
| 6 | Influx bucket/org | Org **`tanadelgigante`** (non kenshiro), bucket **`daylio`**; servizio reale su **192.168.188.120**; in CI si passa `<ip>:<port>` come `vars.INFLUX_INSTANCE` → job `test` costruisce `INFLUX_URL="http://${INFLUX_INSTANCE}"` | **RISOLTO** |
| 7 | `release` job automatico su tag? | **Sempre manuale** via `workflow_dispatch` (come flatpress), MAI automatica/tag | **RISOLTO** |
| 8 | `ingestora_migrations.py` | Rinominare in **`db/migrations.py`** | **RISOLTO** |
| 9 | Scheduler | **APScheduler** (non threading nativo) | **RISOLTO** |

> Nota: la sezione è chiusa; non restano punti aperti su questi aspetti. Eventuali nuove richieste vanno trattate come revisione del WORKPLAN (D12).

---

## 12. Approvazioni e firme

| Ruolo | Nome | Data | Esito |
| --- | --- | --- | --- |
| Utente / Orchestratore | | 2026-09-22 | **Approvato 2026-09-22** — tutti i punti del §11 risolti (D13–D18 confermati) |
| Designer | | 2026-09-22 | Bozza completa aggiornata (conferme + config YAML) |
| Coder | | | |
| Tester | | | |
| Writer | | | |

*Documento approvato 2026-09-22; il piano viene eseguito dalle fasi 0→7 nell'ordine; qualsiasi scostamento richiede una revisione di questo documento (D12).*