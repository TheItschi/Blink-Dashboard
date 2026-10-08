FROM python:3.12-slim

# ffmpeg installieren
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
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

# Persistente Verzeichnisse anlegen
RUN mkdir -p /app/thumb_cache /app/video_cache

EXPOSE 9999

CMD ["python", "app.py"]
