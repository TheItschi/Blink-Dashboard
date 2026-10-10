FROM python:3.12-slim

# ffmpeg für Clip-Vorschaubilder, tzdata für die Zeitzonen-Umrechnung
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Abhängigkeiten zuerst (besseres Layer-Caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App-Dateien kopieren
COPY app.py .
COPY login.html .
COPY dashboard.html .
COPY favicon.svg .
COPY apple-touch-icon.png .

# Persistente Verzeichnisse anlegen
RUN mkdir -p /app/thumb_cache /app/video_cache

EXPOSE 9999

CMD ["python", "app.py"]
