import os
import sqlite3
import pandas as pd
import threading
import time
from flask import Flask, request, jsonify

# Configurazione del database e cartella di dati
DB_PATH = "data.db"
DATA_FOLDER = "data"
MOOD_MAP = {
    "terribile": 0,
    "male": 1,
    "così così": 2,
    "buono": 3,
    "ottimo": 4
}

# Parametri di configurazione letti dalle variabili d'ambiente
IMPORT_INTERVAL = int(os.getenv("IMPORT_INTERVAL", 86400))  # Default: 24 ore
IMPORT_START_HOUR = int(os.getenv("IMPORT_START_HOUR", 0))  # Default: mezzanotte

def wait_until_start_hour():
    """Attende fino all'ora specificata per iniziare la scansione."""
    while True:
        current_hour = time.localtime().tm_hour
        if current_hour == IMPORT_START_HOUR:
            break
        print(f"Attesa fino all'ora di inizio impostata ({IMPORT_START_HOUR}:00). Ora attuale: {current_hour}:00")
        time.sleep(3600)  # Aspetta un'ora prima di controllare di nuovo

def init_db():
    print("Inizializzazione del database...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mood_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            full_date TEXT,
            time TEXT,
            mood INTEGER,
            activities TEXT,
            note_title TEXT,
            note TEXT,
            UNIQUE(user, full_date, time)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mood_map (
            mood_text TEXT PRIMARY KEY,
            mood_value INTEGER
        )
    """)
    
    conn.commit()
    cursor.executemany("INSERT OR IGNORE INTO mood_map VALUES (?, ?)", MOOD_MAP.items())
    conn.commit()
    conn.close()
    print("Database inizializzato con successo.")

def import_data():
    print("Importazione dati dai file CSV...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    for filename in os.listdir(DATA_FOLDER):
        if filename.endswith(".csv"):
            user = filename.replace(".csv", "")
            print(f"Elaborazione file: {filename} per utente: {user}")
            try:
                df = pd.read_csv(os.path.join(DATA_FOLDER, filename))
                df["mood"] = df["mood"].map(MOOD_MAP)
                
                for _, row in df.iterrows():
                    try:
                        cursor.execute(
                            "INSERT INTO mood_entries (user, full_date, time, mood, activities, note_title, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (user, row["full_date"], row["time"], row["mood"], row["activities"], row["note_title"], row["note"])
                        )
                    except sqlite3.IntegrityError:
                        print(f"Duplicato ignorato: {user}, {row['full_date']}, {row['time']}")
            except Exception as e:
                print(f"Errore durante l'elaborazione del file {filename}: {e}")
    
    conn.commit()
    conn.close()
    print("Importazione dati completata.")

def scheduled_import():
    wait_until_start_hour()
    while True:
        print("Esecuzione importazione dati programmata...")
        import_data()
        print(f"Attesa {IMPORT_INTERVAL} secondi per la prossima importazione...")
        time.sleep(IMPORT_INTERVAL)

app = Flask(__name__)

@app.route("/users", methods=["GET"])
def get_users():
    print("Richiesta ricevuta: /users")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user, COUNT(*) FROM mood_entries GROUP BY user")
    users = cursor.fetchall()
    conn.close()
    print(f"Utenti trovati: {users}")
    return jsonify([{"user": u, "records": r} for u, r in users])

@app.route("/moods", methods=["GET"])
def get_moods():
    user = request.args.get("user")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    print(f"Richiesta ricevuta: /moods per user={user}, start_date={start_date}, end_date={end_date}")
    
    query = "SELECT full_date, time, mood, activities, note_title, note FROM mood_entries WHERE user = ?"
    params = [user]
    if start_date:
        query += " AND full_date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND full_date <= ?"
        params.append(end_date)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(query, params)
    moods = cursor.fetchall()
    conn.close()
    
    print(f"Mood trovati: {moods}")
    return jsonify([{"date": d, "time": t, "mood": m, "activities": a, "note_title": nt, "note": n} for d, t, m, a, nt, n in moods])

if __name__ == "__main__":
    print("Avvio del server...")
    init_db()
    import_data()
    
    print("Avvio del thread per l'importazione automatica dei dati...")
    threading.Thread(target=scheduled_import, daemon=True).start()
    
    print("Server in esecuzione su http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000)
