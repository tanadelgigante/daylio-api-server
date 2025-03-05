# DaylioApiServer

DaylioApiServer è un server REST che importa dati da file CSV in un database SQLite, evitando i duplicati e offrendo API REST per interrogare i dati.

## Funzionalità
- Importazione automatica dei file CSV da una cartella predefinita.
- Memorizzazione dei dati in un database SQLite con chiavi uniche per evitare duplicati.
- Conversione del "mood" in valori numerici secondo una tabella di decodifica.
- API REST per interrogare i dati:
  - Lista dei mood di un utente in un intervallo di date.
  - Calcolo del mood medio in un intervallo di date.
  - Elenco degli utenti presenti nel database.

## Requisiti
- Docker
- Python 3.9+

## Installazione
1. Clonare il repository:
   ```sh
   git clone <repo-url>
   cd DaylioApiServer
   ```

2. Costruire e avviare il container Docker:
   ```sh
   docker build -t daylio-api .
   docker run -p 5000:5000 -v $(pwd)/data:/app/data daylio-api
   ```

## API

### 1. Lista degli utenti
**GET** `/users`
Restituisce l'elenco degli utenti presenti nel database con il numero di record disponibili.

### 2. Lista dei mood di un utente
**GET** `/moods?user=USERNAME&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
Ritorna i mood dell'utente tra due date opzionali.

### 3. Mood medio di un utente
**GET** `/average_mood?user=USERNAME&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
Calcola il mood medio dell'utente nell'intervallo di date fornito.

## Struttura dei dati
Ogni file CSV deve avere il seguente formato:
```
full_date,time,mood
2025-03-01,08:30,buono
2025-03-02,12:00,male
```

## Note
- Il server avvia automaticamente l'importazione dei dati dai file CSV presenti nella cartella `data/`.
