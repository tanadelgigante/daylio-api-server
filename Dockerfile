# Utilizza un'immagine base di Python
FROM python:3.9

# Imposta la directory di lavoro
WORKDIR /app

# Copia i file del progetto
COPY requirements.txt .
COPY . .

# Installa le dipendenze
RUN pip install --no-cache-dir -r requirements.txt

# Espone la porta per Flask
EXPOSE 5000

# Comando di avvio
CMD ["python", "server.py"]