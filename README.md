# Blink Dashboard

Lokale Web-Oberfläche für Blink-Kameras mit Sync Module 2 (lokaler USB-Speicher).
Clips werden nach Aktualität sortiert angezeigt, lassen sich im Browser abspielen,
herunterladen und löschen.

![Lizenz](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)

---

## Hinweis

Dies ist ein privates Hobbyprojekt ohne jede Verbindung zu Blink, Immedia
Semiconductor oder Amazon. Es wird weder von diesen unterstützt, geprüft noch
genehmigt. „Blink" ist eine Marke der jeweiligen Rechteinhaber und wird hier
ausschließlich beschreibend verwendet.

Das Projekt nutzt die inoffizielle Bibliothek
[blinkpy](https://github.com/fronzbot/blinkpy). Ändert Blink seine API, kann die
Anwendung jederzeit aufhören zu funktionieren. Nutzung auf eigene Verantwortung.

---

## Schnellstart

### Docker (empfohlen)

```bash
docker compose up --build -d
# → http://localhost:9999
```

ffmpeg ist im Image enthalten; Anmeldedaten und Caches liegen unter `./data/`
und überstehen einen Neustart.

### Direkt mit Python

```bash
pip install -r requirements.txt
python app.py
# → http://localhost:9999
```

Für Clip-Vorschaubilder wird [ffmpeg](https://ffmpeg.org/download.html) benötigt
— entweder im `PATH` oder als `ffmpeg/ffmpeg.exe` neben `app.py`. Fehlt es,
fällt die App auf das Kamera-Thumbnail zurück.

Beim ersten Start erscheint die Anmeldemaske. Nach Login und 2FA-Bestätigung
wird die Sitzung lokal gespeichert; der Token wird danach automatisch erneuert.

---

## Funktionen

| | |
|---|---|
| Anmeldung | E-Mail/Passwort + 2FA im Browser, Token-Erneuerung im Hintergrund |
| Clip-Übersicht | Nach Aufnahmezeit sortiert, gruppiert und filterbar nach Kamera |
| Vorschaubilder | Erster Frame jedes Clips, per ffmpeg erzeugt und gecacht |
| Wiedergabe | Modal-Player mit Range-Request-Unterstützung (auch iOS Safari) |
| Download | Einzelne Clips als `.mp4` |
| Löschen | Einzeln oder alle Clips, Massenlöschen mit Fortschrittsanzeige |
| Scharfschaltung | System scharf/unscharf (entspricht dem Schloss-Symbol in der Blink-App) |
| Bewegungserkennung | Pro Kamera ein-/ausschaltbar |
| Kamera-Status | Batteriespannung und WLAN-Stärke |
| Automatik | Clips und Status werden alle 60 Sekunden abgeglichen, mit Countdown |
| Mobil | Responsives Layout für Smartphone-Browser |

---

## Einbindung in andere Seiten

Das aktuellste Vorschaubild einer Kamera lässt sich direkt einbetten:

```html
<img src="http://localhost:9999/proxy/thumb/camera?name=Garten">
<img src="http://localhost:9999/thumb/latest">
```

---

## Konfiguration

Alle Einstellungen stehen als Konstanten oben in `app.py`:

| Konstante | Standard | Bedeutung |
|---|---|---|
| `PORT` | `9999` | Port des Webservers |
| `POLL_INTERVAL_SECONDS` | `60` | Abgleich-Intervall für Clips und Status |
| `THUMB_CACHE_SECONDS` | `300` | Gültigkeit des Kamera-Thumbnail-Caches |

Die **Zeitzone** wird über die Umgebungsvariable `TZ` gesetzt (in
`docker-compose.yml` vorbelegt mit `Europe/Berlin`). Blink liefert alle
Zeitstempel in UTC; ohne passende Zone zeigt die Übersicht die Aufnahmezeiten
um den UTC-Versatz verschoben an. Sommer- und Winterzeit werden automatisch
berücksichtigt. Ohne gesetztes `TZ` gilt die Systemzeit — im Docker-Container
ist das UTC.

---

## Projektstruktur

```
app.py               FastAPI-Backend, Blink-Anbindung, Medien-Proxy
login.html           Anmeldung und 2FA
dashboard.html       Clip-Übersicht
favicon.svg
requirements.txt
Dockerfile
docker-compose.yml
```

Zur Laufzeit entstehen zusätzlich `blink_credentials.json`, `thumb_cache/`,
`video_cache/` und `blink_webapp.log` (bzw. alles unter `data/` im Container).
Diese Dateien sind in `.gitignore` ausgeschlossen und gehören nicht ins
Repository — `blink_credentials.json` enthält ein gültiges Zugriffstoken.
Das Passwort wird bewusst **nicht** gespeichert; für die Token-Erneuerung
genügt der Refresh-Token.

---

## Technische Hinweise

**Kein rein lokaler Betrieb.** Das Sync Module 2 besitzt keinen eigenen
HTTP-Server. Clips liegen zwar lokal auf dem USB-Stick, werden aber über
Blinks Relay-Server abgerufen; eine Internetverbindung ist daher nötig. Beim
Abspielen wird ein Clip kurzzeitig über diesen Relay bereitgestellt, nicht
dauerhaft in die Cloud hochgeladen.

**Keine Push-Benachrichtigungen.** Blink bietet dafür keine öffentlich
nutzbare Schnittstelle, deshalb wird im 60-Sekunden-Takt abgefragt.

**Zugriffsschutz.** Die Anwendung besitzt keine eigene Benutzerverwaltung.
Wer den Port erreicht, sieht die Clips. Nur im vertrauenswürdigen Netz
betreiben und nicht ungeschützt ins Internet stellen.

---

## Fehlerbehebung

| Problem | Lösung |
|---|---|
| Keine Clips sichtbar | Zeitraum erhöhen oder „Aktualisieren" klicken |
| Vorschaubilder fehlen | ffmpeg installieren bzw. in `ffmpeg/` ablegen |
| Erneute Anmeldung nötig | `data/blink_credentials.json` löschen und neu anmelden |
| Port belegt | `PORT` in `app.py` und den Port in `docker-compose.yml` anpassen |
| Aufnahmezeiten verschoben | `TZ` in `docker-compose.yml` auf die eigene Zeitzone setzen |
| Änderungen greifen nicht | `docker compose up --build -d` (nicht nur `up`) |

---

## Lizenz

[MIT](LICENSE) — © 2026 Yavuz Icsezer
