"""
Blink Outdoor 4 – Web-Dashboard
Startet einen lokalen Webserver mit Videoübersicht und Stream/Download.

Installation:
    pip install -r requirements.txt

Start:
    python app.py
Dann im Browser öffnen: http://localhost:9999
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict

import uvicorn
from aiohttp import ClientSession
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from blinkpy.blinkpy import Blink, BlinkTwoFARequiredError
from blinkpy.auth import Auth, UnauthorizedError, TokenRefreshFailed, LoginError

# ──────────────────────────────────────────────
#  Pfade & Logging
# ──────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
DATA_DIR  = Path(os.environ.get("DATA_DIR", BASE_DIR))  # überschreibbar per Env-Var

CREDENTIALS_FILE = DATA_DIR / "blink_credentials.json"
TEMPLATES_DIR    = BASE_DIR          # HTML-Dateien liegen direkt neben app.py
STATIC_DIR       = BASE_DIR / "static"
CACHE_DIR        = DATA_DIR / "video_cache"
LOG_FILE         = DATA_DIR / "blink_webapp.log"
THUMB_CACHE_DIR_DEFAULT = DATA_DIR / "thumb_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
THUMB_CACHE_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────
#  Konfiguration
# ──────────────────────────────────────────────
PORT = 9999   # Web-Server Port – hier ändern falls gewünscht

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Globaler Blink-State
# ──────────────────────────────────────────────
_blink: Optional[Blink] = None
_session: Optional[ClientSession] = None
_pending_2fa: bool          = False
_session_was_invalidated: bool = False  # True, wenn die Session wegen Ablauf verworfen wurde
_video_cache: List[Dict]    = []
_last_refresh: Optional[datetime] = None
_thumb_cache: Dict[str, bytes] = {}   # camera_name -> JPEG bytes
# ──────────────────────────────────────────────
#  Zentrale Poll-Konfiguration
# ──────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 60  # Einheitliches Intervall für Clip-Sync, Arm-/Motion-Status (Back- & Frontend)
CACHE_TTL_SECONDS = POLL_INTERVAL_SECONDS  # Video-Cache-TTL an Poll-Intervall angeglichen
THUMB_CACHE_SECONDS = 300  # Auffrisch-Intervall für das gespeicherte Fallback-Thumbnail
THUMB_CACHE_DIR = THUMB_CACHE_DIR_DEFAULT

# ──────────────────────────────────────────────
#  FastAPI-App
# ──────────────────────────────────────────────
app = FastAPI(title="Blink Dashboard")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ──────────────────────────────────────────────
#  Blink Auth-Hilfsfunktionen
# ──────────────────────────────────────────────

def load_credentials() -> Optional[Dict]:
    if CREDENTIALS_FILE.exists():
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_credentials(blink: Blink) -> None:
    cred = blink.auth.login_attributes
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        json.dump(cred, f, indent=2)
    log.info("Credentials gespeichert.")


def _make_token_callback(auth: Auth):
    """
    Callback für blinkpy: wird aufgerufen, sobald die Bibliothek intern den
    Token erneuert hat. Blink rotiert dabei auch den Refresh-Token – ohne
    Speichern wäre der auf der Platte veraltet und würde später abgelehnt
    (Folge: erneute Anmeldung inkl. 2FA).
    """
    def _cb() -> None:
        try:
            with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
                json.dump(auth.login_attributes, f, indent=2)
            log.info("Token erneuert – Credentials aktualisiert.")
        except Exception as exc:
            log.warning("Token-Callback: Speichern fehlgeschlagen: %s", exc)
    return _cb


def _attach_token_callback(blink: Blink) -> None:
    """Verknüpft den Persistenz-Callback mit der Auth-Instanz."""
    try:
        blink.auth.callback = _make_token_callback(blink.auth)
    except Exception as exc:
        log.warning("Token-Callback konnte nicht gesetzt werden: %s", exc)


def is_logged_in() -> bool:
    return _blink is not None and not _pending_2fa


def _is_auth_error(exc: Exception) -> bool:
    """
    Prüft, ob eine Exception wirklich eine ungültige Anmeldung bedeutet.

    Bewusst eng gefasst: Netzwerk-Aussetzer, Timeouts oder kurzzeitige
    Blink-Serverfehler dürfen NICHT zum Löschen der gespeicherten Anmeldung
    führen – sonst wäre bei jeder Störung ein neuer 2FA-Login nötig.
    """
    return isinstance(exc, (UnauthorizedError, BlinkTwoFARequiredError))


async def invalidate_session(reason: str = "") -> None:
    """Session verwerfen, damit die App zur Anmeldemaske zurückkehrt."""
    global _blink, _session, _pending_2fa, _video_cache, _last_refresh
    global _session_was_invalidated
    log.warning("Session ungültig – Neuanmeldung erforderlich. %s", reason)
    _session_was_invalidated = True
    CREDENTIALS_FILE.unlink(missing_ok=True)
    if _session and not _session.closed:
        try:
            await _session.close()
        except Exception:
            pass
    _blink        = None
    _session      = None
    _pending_2fa  = False
    _video_cache  = []
    _last_refresh = None


async def handle_possible_auth_error(exc: Exception) -> bool:
    """Verwirft die Session, wenn die Exception auf einen Auth-Fehler hindeutet.
    Gibt True zurück, wenn die Session verworfen wurde."""
    if _is_auth_error(exc):
        await invalidate_session(f"Auslöser: {exc}")
        return True
    return False


async def ensure_token_fresh() -> bool:
    """Erneuert den Zugriffstoken, bevor er abläuft.
    Gibt False zurück, wenn eine komplette Neuanmeldung nötig ist."""
    if _blink is None or _blink.auth is None:
        return False
    try:
        if _blink.auth.need_refresh():
            log.info("Token läuft ab – wird erneuert …")
            await _blink.auth.refresh_tokens(refresh=True)
            save_credentials(_blink)
            log.info("Token erfolgreich erneuert.")
        return True
    except (UnauthorizedError, BlinkTwoFARequiredError) as exc:
        # Blink lehnt den Refresh-Token ab -> Neuanmeldung unvermeidbar
        await invalidate_session(f"Refresh-Token abgelehnt: {exc}")
        return False
    except Exception as exc:
        # Netzwerkproblem o.ä. – Anmeldung behalten, beim nächsten Mal erneut versuchen
        log.warning("Token-Erneuerung vorübergehend fehlgeschlagen: %s", exc)
        return True


async def try_restore_session() -> bool:
    """Versucht, eine gespeicherte Session wiederherzustellen.
    Bei vorübergehenden Fehlern (Netzwerk beim Container-Start noch nicht bereit)
    werden mehrere Versuche unternommen, ohne die Anmeldung zu verwerfen."""
    global _blink, _session, _pending_2fa
    saved = load_credentials()
    if not saved:
        return False

    for attempt in range(3):
        try:
            _session = ClientSession()
            blink    = Blink(session=_session)
            auth     = Auth(saved, no_prompt=True)
            blink.auth = auth
            _attach_token_callback(blink)
            await blink.start()
            _blink      = blink
            _pending_2fa = False
            log.info("Session aus Credentials wiederhergestellt.")
            return True

        except (UnauthorizedError, BlinkTwoFARequiredError) as exc:
            # Blink lehnt die gespeicherte Anmeldung endgültig ab
            log.warning("Gespeicherte Anmeldung ungültig (%s) – Neuanmeldung nötig.", type(exc).__name__)
            CREDENTIALS_FILE.unlink(missing_ok=True)
            if _session and not _session.closed:
                await _session.close()
            _blink = None
            return False

        except Exception as exc:
            # Vorübergehendes Problem – Credentials NICHT löschen
            log.warning("Session-Wiederherstellung Versuch %d/3 fehlgeschlagen: %s",
                        attempt + 1, exc)
            if _session and not _session.closed:
                await _session.close()
            _session = None
            _blink   = None
            if attempt < 2:
                await asyncio.sleep(5 * (attempt + 1))

    log.warning("Session konnte nicht wiederhergestellt werden – Anmeldung bleibt gespeichert, "
                "es wird beim nächsten Poll erneut versucht.")
    return False


async def start_login(email: str, password: str) -> dict:
    """Startet den Login-Prozess. Gibt {'ok': True} oder {'needs_2fa': True} zurück."""
    global _blink, _session, _pending_2fa
    if _session and not _session.closed:
        await _session.close()
    _session = ClientSession()
    blink    = Blink(session=_session)
    auth     = Auth({"username": email, "password": password}, no_prompt=True)
    blink.auth = auth
    _attach_token_callback(blink)
    try:
        await blink.start()
        _blink       = blink
        _pending_2fa = False
        globals()["_session_was_invalidated"] = False
        save_credentials(blink)
        return {"ok": True}
    except BlinkTwoFARequiredError:
        # 2FA erforderlich – Session offen lassen für den PIN-Schritt
        _blink       = blink
        _pending_2fa = True
        log.info("2FA erforderlich – warte auf PIN.")
        return {"needs_2fa": True}
    except Exception as exc:
        log.error("Login-Fehler: %s", exc)
        if _session and not _session.closed:
            await _session.close()
        _blink       = None
        _pending_2fa = False
        return {"error": str(exc) or "Anmeldung fehlgeschlagen. Bitte Zugangsdaten prüfen."}


async def submit_2fa(pin: str) -> dict:
    global _blink, _pending_2fa
    if _blink is None:
        return {"error": "Kein laufender Login-Vorgang."}
    try:
        success = await _blink.auth.complete_2fa_login(pin)
        if not success:
            return {"error": "Ungültiger Code. Bitte erneut versuchen."}
        # URLs initialisieren (wird normalerweise in Blink.start() gemacht,
        # das bei 2FA aber vorzeitig abbricht)
        _blink.setup_urls()
        await _blink.setup_post_verify()
        _pending_2fa = False
        globals()["_session_was_invalidated"] = False
        save_credentials(_blink)
        return {"ok": True}
    except Exception as exc:
        log.error("2FA-Fehler: %s", exc)
        return {"error": str(exc)}


# ──────────────────────────────────────────────
#  Video-Abruf
# ──────────────────────────────────────────────

async def fetch_videos(days: int = 30) -> List[Dict]:
    global _video_cache, _last_refresh
    now = datetime.now(timezone.utc)
    if (
        _video_cache
        and _last_refresh
        and (now - _last_refresh).total_seconds() < CACHE_TTL_SECONDS
    ):
        return _video_cache

    if _blink is None:
        return []

    cutoff = now - timedelta(days=days)
    videos: List[Dict] = []

    for sync_name, sync in _blink.sync.items():
        log.info("Sync Module '%s': local_storage=%s", sync_name, sync.local_storage)

        # ── Local Storage (Sync Module 2) ──────────────────
        if sync.local_storage:
            log.info("Lade lokales Manifest für '%s' …", sync_name)
            # Manifest leeren, damit gelöschte Clips nicht weiter angezeigt werden.
            # update_local_storage_manifest() fügt nur hinzu, entfernt aber nie.
            sync._local_storage["manifest"] = set()
            try:
                await sync.update_local_storage_manifest()
            except Exception as exc:
                log.error("Manifest-Fehler für '%s': %s", sync_name, exc)
                if await handle_possible_auth_error(exc):
                    return []

            manifest = sync._local_storage.get("manifest", set())
            manifest_id = sync._local_storage.get("last_manifest_id")
            log.info("Manifest enthält %d Clips (manifest_id=%s)", len(manifest), manifest_id)

            # Kamera-Thumbnails vorab laden (mit frischer Thumbnail-URL)
            for cam_name, cam_obj in sync.cameras.items():
                safe = "".join(c if c.isalnum() else "_" for c in cam_name)
                cache_file = THUMB_CACHE_DIR / f"{safe}.jpg"
                if not cache_file.exists() or (datetime.now(timezone.utc).timestamp() - cache_file.stat().st_mtime) > THUMB_CACHE_SECONDS:
                    try:
                        # Konfiguration frisch holen, damit cam_obj.thumbnail den
                        # aktuellen ts-Zeitstempel enthält
                        info = await sync.get_camera_info(cam_obj.camera_id)
                        if info:
                            await cam_obj.update(info, force_cache=True, expire_clips=False)
                        if cam_obj.thumbnail:
                            from blinkpy.api import http_get as _hg
                            r = await _hg(_blink, cam_obj.thumbnail, stream=False, json=False)
                            if r and r.status == 200:
                                data = await r.read()
                                cache_file.write_bytes(data)
                                cam_obj._cached_image = data
                    except Exception as te:
                        log.warning("Thumbnail-Fehler für Kamera '%s': %s", cam_name, te)

            for item in manifest:
                try:
                    created = item.created_at  # datetime object
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    if created < cutoff:
                        continue
                    videos.append({
                        "device_name":  item.name,
                        "created_at":   created.isoformat(),
                        "_ts_display":  created.strftime("%d.%m.%Y  %H:%M:%S"),
                        "_ts_sort":     created.isoformat(),
                        "_item":        item,
                        "_manifest_id": manifest_id,
                        "_clip_id":     item.id,
                        "_sync_name":   sync_name,
                        "_proxy_url":   f"/proxy/local?clip_id={item.id}&sync={sync_name}",
                        "_thumb_url":   f"/proxy/thumb/clip?clip_id={item.id}&sync={sync_name}",
                        "media":        f"/local/{sync_name}/{item.id}",
                    })
                except Exception as exc:
                    log.warning("Fehler bei Clip-Item: %s", exc)

        # ── Cloud-Storage (Fallback) ────────────────────────
        else:
            log.info("Sync Module '%s' hat kein Local Storage – versuche Cloud-API.", sync_name)
            try:
                since_dt  = now - timedelta(days=days)
                since_str = since_dt.strftime("%Y/%m/%d %H:%M:%S")
                cloud_vids = await _blink.get_videos_metadata(since=since_str, stop=20)
                log.info("Cloud lieferte %d Videos.", len(cloud_vids))
                for v in cloud_vids:
                    raw = v.get("created_at", "")
                    try:
                        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt < cutoff:
                            continue
                        v["_ts_display"] = dt.strftime("%d.%m.%Y  %H:%M:%S")
                        v["_ts_sort"]    = dt.isoformat()
                        v["_proxy_url"]  = video_proxy_url(v.get("media", ""))
                        v["_thumb_url"]  = thumb_proxy_url(v.get("thumbnail", "")) if v.get("thumbnail") else ""
                    except Exception:
                        v["_ts_display"] = raw
                        v["_ts_sort"]    = raw
                    videos.append(v)
            except Exception as exc:
                log.error("Cloud-API Fehler: %s", exc)

    # Sortieren: neueste zuerst
    videos.sort(key=lambda v: v.get("_ts_sort", ""), reverse=True)

    _video_cache  = videos
    _last_refresh = now
    log.info("%d Videos insgesamt geladen.", len(videos))
    return videos


def video_proxy_url(media_path: str) -> str:
    """Baut die interne Proxy-URL für ein Video."""
    import urllib.parse
    return "/proxy/video?path=" + urllib.parse.quote(media_path, safe="")


def thumb_proxy_url(thumb_path: str) -> str:
    import urllib.parse
    return "/proxy/thumb?path=" + urllib.parse.quote(thumb_path, safe="")


# ──────────────────────────────────────────────
#  Startup: Session wiederherstellen
# ──────────────────────────────────────────────
_poll_task: Optional[asyncio.Task] = None


async def _background_poll_loop():
    """Aktualisiert das Video-Manifest periodisch im Hintergrund (Polling statt Push,
    da Blink keine öffentlich nutzbare Push-Notification-API anbietet)."""
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            # Nicht angemeldet, aber Credentials vorhanden -> erneut versuchen
            if not is_logged_in() and CREDENTIALS_FILE.exists():
                log.info("Erneuter Versuch, die gespeicherte Session wiederherzustellen …")
                await try_restore_session()

            if is_logged_in():
                global _last_refresh
                # Token proaktiv erneuern, bevor er abläuft
                await ensure_token_fresh()
                if not is_logged_in():
                    continue  # Session wurde verworfen -> Anmeldung nötig
                _last_refresh = None  # Cache invalidieren, erzwingt echten Refresh
                count = len(await fetch_videos())
                log.info("Hintergrund-Refresh: %d Videos.", count)
        except Exception as exc:
            log.warning("Hintergrund-Refresh fehlgeschlagen: %s", exc)
            await handle_possible_auth_error(exc)
            await handle_possible_auth_error(exc)


@app.on_event("startup")
async def on_startup():
    global _poll_task
    await try_restore_session()
    _poll_task = asyncio.create_task(_background_poll_loop())


@app.on_event("shutdown")
async def on_shutdown():
    if _poll_task:
        _poll_task.cancel()
    if _session and not _session.closed:
        await _session.close()


# ──────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import Response
    svg = (BASE_DIR / "favicon.svg").read_bytes() if (BASE_DIR / "favicon.svg").exists() else b""
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not is_logged_in():
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "pending_2fa": _pending_2fa,
                "session_expired": _session_was_invalidated,
            },
        )
    return RedirectResponse("/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, days: int = 30):
    if not is_logged_in():
        return RedirectResponse("/")

    # Token proaktiv erneuern; bei abgelaufener Session zurück zur Anmeldung
    await ensure_token_fresh()
    if not is_logged_in():
        return RedirectResponse("/")

    videos  = await fetch_videos(days=days)

    # fetch_videos kann die Session bei Auth-Fehlern verwerfen
    if not is_logged_in():
        return RedirectResponse("/")

    # Alle bekannten Kameras aus dem Blink-System (unabhängig von vorhandenen Clips)
    all_cameras = set()
    if _blink:
        for sync in _blink.sync.values():
            all_cameras.update(sync.cameras.keys())
    # Kameras aus den Videos mit einbeziehen (falls eine Kamera nicht mehr im System ist)
    all_cameras.update(v.get("device_name", "Unbekannt") for v in videos)
    cameras = sorted(all_cameras)
    sync_names = sorted(_blink.sync.keys()) if _blink else []

    # Gruppen nach Kamera
    by_camera: Dict[str, list] = {c: [] for c in cameras}
    for v in videos:
        cam = v.get("device_name", "Unbekannt")
        by_camera.setdefault(cam, []).append(v)

    # Proxy-URLs nur setzen wenn nicht schon von fetch_videos gesetzt (Local Storage)
    for v in videos:
        if "_proxy_url" not in v:
            v["_proxy_url"] = video_proxy_url(v.get("media", ""))
        if "_thumb_url" not in v:
            thumb = v.get("thumbnail", "")
            v["_thumb_url"] = thumb_proxy_url(thumb) if thumb else ""
        # Download-URL
        if "_item" in v:
            # Local Storage Clip
            import urllib.parse
            cam_safe = urllib.parse.quote(v.get("device_name", "kamera"))
            ts_safe  = v.get("_ts_display", "")[:10].replace(".", "-")
            v["_download_url"] = f"/proxy/local/download?clip_id={v['_clip_id']}&sync={urllib.parse.quote(v.get('_sync_name', list(_blink.sync.keys())[0]))}&filename={cam_safe}_{ts_safe}.mp4"
        else:
            import urllib.parse
            cam_safe = urllib.parse.quote(v.get("device_name", "kamera"))
            ts_safe  = v.get("_ts_display", "")[:10].replace(".", "-")
            v["_download_url"] = f"/proxy/download?path={urllib.parse.quote(v.get('media', ''))}&filename={cam_safe}_{ts_safe}.mp4"

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "videos":    videos,
            "cameras":   cameras,
            "sync_names": sync_names,
            "by_camera": by_camera,
            "days":      days,
            "total":     len(videos),
            "poll_interval": POLL_INTERVAL_SECONDS,
            "last_refresh_epoch": _last_refresh.timestamp() if _last_refresh else None,
        },
    )


# ── Login-API ──────────────────────────────────

@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    result = await start_login(body.get("email", ""), body.get("password", ""))
    return JSONResponse(result)


@app.post("/api/2fa")
async def api_2fa(request: Request):
    body = await request.json()
    result = await submit_2fa(body.get("pin", ""))
    return JSONResponse(result)


@app.post("/api/logout")
async def api_logout():
    global _blink, _session, _pending_2fa, _video_cache, _last_refresh
    if _session and not _session.closed:
        await _session.close()
    _blink        = None
    _session      = None
    _pending_2fa  = False
    _video_cache  = []
    _last_refresh = None
    CREDENTIALS_FILE.unlink(missing_ok=True)
    return JSONResponse({"ok": True})


@app.post("/api/refresh")
async def api_refresh():
    global _last_refresh
    _last_refresh = None  # Cache leeren
    await fetch_videos()
    return JSONResponse({"ok": True, "count": len(_video_cache)})


@app.get("/api/status")
async def api_status():
    cams = []
    syncs = []

    # Token proaktiv erneuern; schlägt das fehl, ist eine Neuanmeldung nötig
    if _blink is not None:
        await ensure_token_fresh()

    if not is_logged_in():
        return JSONResponse(
            {"logged_in": False, "cameras": [], "syncs": [], "session_expired": True},
            status_code=401,
        )

    if _blink:
        # Netzwerk-Info aktualisieren, damit sync.arm (System scharf/unscharf) aktuell ist
        for sync_name, sync in _blink.sync.items():
            try:
                await sync.get_network_info()
            except Exception as exc:
                log.warning("Netzwerk-Refresh für '%s' fehlgeschlagen: %s", sync_name, exc)
                if await handle_possible_auth_error(exc):
                    return JSONResponse(
                        {"logged_in": False, "cameras": [], "syncs": [], "session_expired": True},
                        status_code=401,
                    )

            syncs.append({"name": sync_name, "armed": bool(sync.arm)})

            for cam_name, cam in sync.cameras.items():
                # Kamera-Info direkt und ungecacht von der Blink-API abrufen,
                # damit Änderungen aus der offiziellen App (z.B. motion_enabled)
                # sofort erkannt werden.
                camera_info = None
                try:
                    camera_info = await sync.get_camera_info(cam.camera_id)
                    if camera_info:
                        await cam.update(camera_info, force_cache=True, expire_clips=False)
                except Exception as exc:
                    log.warning("Kamera-Refresh für '%s' fehlgeschlagen: %s", cam_name, exc)
                    await handle_possible_auth_error(exc)

                a = cam.attributes
                cams.append({
                    "sync":    sync_name,
                    "name":    cam_name,
                    "battery": a.get("battery_voltage", "n/a"),
                    "wifi":    a.get("wifi_strength", "n/a"),
                    "motion":  a.get("motion_enabled", False),
                    # 'armed' = System-weiter Scharf/Unscharf-Status (Schloss-Symbol in der App).
                    "armed":   bool(sync.arm),
                })
    return JSONResponse({"logged_in": is_logged_in(), "cameras": cams, "syncs": syncs})


@app.post("/api/sync/arm")
async def api_sync_arm(request: Request):
    """System scharf/unscharf schalten (entspricht dem Schloss-Symbol in der Blink-App)."""
    if _blink is None:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    body     = await request.json()
    sync_name = body.get("sync", "")
    enable    = bool(body.get("enable", True))

    sync = _blink.sync.get(sync_name)
    if sync is None:
        raise HTTPException(status_code=404, detail=f"Sync Module '{sync_name}' nicht gefunden")

    try:
        await sync.async_arm(enable)
        log.info("System '%s': %s", sync_name, "scharf geschaltet" if enable else "unscharf geschaltet")
        return JSONResponse({"ok": True, "sync": sync_name, "armed": enable})
    except Exception as exc:
        log.error("Fehler beim Umschalten von Sync Module '%s': %s", sync_name, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/camera/motion")
async def api_camera_motion(request: Request):
    """Bewegungserkennung einer einzelnen Kamera aktivieren oder deaktivieren
    (Pro-Kamera-Einstellung, unabhängig vom System-weiten Scharf/Unscharf-Status)."""
    if _blink is None:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    body      = await request.json()
    cam_name  = body.get("camera", "")
    enable    = bool(body.get("enable", True))

    camera = None
    for sync in _blink.sync.values():
        if cam_name in sync.cameras:
            camera = sync.cameras[cam_name]
            break

    if camera is None:
        raise HTTPException(status_code=404, detail=f"Kamera '{cam_name}' nicht gefunden")

    try:
        await camera.async_arm(enable)
        # Lokalen Zustand sofort aktualisieren, damit die UI ohne Verzögerung stimmt
        camera.motion_enabled = enable
        log.info("Bewegungserkennung für '%s': %s", cam_name, "aktiviert" if enable else "deaktiviert")
        return JSONResponse({"ok": True, "camera": cam_name, "motion_enabled": enable})
    except Exception as exc:
        log.error("Fehler beim Umschalten der Bewegungserkennung für '%s': %s", cam_name, exc)
        raise HTTPException(status_code=500, detail=str(exc))


# ── Video-Proxy ───────────────────────────────

async def _proxy_stream(blink_path: str, content_type: str):
    """Streamt Blink-Mediendaten durch den lokalen Server."""
    if _blink is None:
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    from blinkpy.api import http_get
    base = _blink.urls.base_url if _blink.urls else "https://rest-prod.immedia-semi.com"
    url  = base + blink_path if blink_path.startswith("/") else blink_path

    response = await http_get(_blink, url, stream=True, json=False)
    if response is None or response.status != 200:
        raise HTTPException(status_code=502, detail="Blink API Fehler")

    async def generator():
        async for chunk in response.content.iter_chunked(65536):
            yield chunk

    return StreamingResponse(generator(), media_type=content_type)


@app.get("/proxy/local")
async def proxy_local(clip_id: int, sync: str, request: Request):
    """
    Streamt einen Clip aus dem lokalen Sync-Module-Speicher.
    Unterstützt Range-Requests (erforderlich für Safari/iOS).
    """
    if _blink is None:
        raise HTTPException(status_code=401)
    sync_module = _blink.sync.get(sync)
    if sync_module is None:
        raise HTTPException(status_code=404, detail=f"Sync Module '{sync}' nicht gefunden")

    manifest = sync_module._local_storage.get("manifest", set())
    item = next((i for i in manifest if i.id == clip_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} nicht im Manifest")

    try:
        await item.prepare_download(_blink)
        manifest_id = sync_module._local_storage.get("last_manifest_id")
        url = _blink.urls.base_url + item.url(manifest_id)
        from blinkpy.api import http_get
        response = await http_get(_blink, url, stream=False, json=False)
        if response is None or response.status != 200:
            raise HTTPException(status_code=502, detail="Clip konnte nicht geladen werden")

        data = await response.read()
        return _range_response(data, request, "video/mp4")

    except HTTPException:
        raise
    except Exception as exc:
        log.error("Fehler beim Laden von Local-Storage-Clip %d: %s", clip_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


def _range_response(data: bytes, request: Request, content_type: str):
    """
    Gibt eine HTTP-Response mit Range-Request-Support zurück.
    Safari benötigt das für die <video>-Wiedergabe.
    """
    total = len(data)
    range_header = request.headers.get("range")

    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": content_type,
    }

    if range_header:
        # Range: bytes=START-END
        try:
            range_val = range_header.strip().replace("bytes=", "")
            start_str, end_str = range_val.split("-")
            start = int(start_str) if start_str else 0
            end   = int(end_str)   if end_str   else total - 1
            end   = min(end, total - 1)
            chunk = data[start:end + 1]
            headers = {
                **base_headers,
                "Content-Range":  f"bytes {start}-{end}/{total}",
                "Content-Length": str(len(chunk)),
            }
            return Response(content=chunk, status_code=206, headers=headers)
        except Exception:
            pass  # Ungültiger Range → vollständige Antwort

    # Vollständige Antwort
    return Response(
        content=data,
        status_code=200,
        headers={**base_headers, "Content-Length": str(total)},
    )


@app.get("/proxy/local/download")
async def proxy_local_download(clip_id: int, sync: str, filename: str = "clip.mp4"):
    """Download eines Local-Storage-Clips."""
    if _blink is None:
        raise HTTPException(status_code=401)
    sync_module = _blink.sync.get(sync)
    if sync_module is None:
        raise HTTPException(status_code=404)

    manifest = sync_module._local_storage.get("manifest", set())
    item = next((i for i in manifest if i.id == clip_id), None)
    if item is None:
        raise HTTPException(status_code=404)

    try:
        await item.prepare_download(_blink)
        manifest_id = sync_module._local_storage.get("last_manifest_id")
        url = _blink.urls.base_url + item.url(manifest_id)
        from blinkpy.api import http_get
        response = await http_get(_blink, url, json=False)
        if response is None or response.status != 200:
            raise HTTPException(status_code=502)
        data = await response.read()
        return Response(
            content=data,
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


async def _refresh_manifest(sync_module) -> Optional[str]:
    """
    Lädt das lokale Manifest neu und liefert die aktuelle manifest_id.

    Das Sync Module erzeugt sein Manifest nach jeder Änderung (z.B. einem
    Löschvorgang) neu. Die in den Clip-Objekten gespeicherte manifest_id ist
    danach veraltet – Löschanfragen damit laufen ins Leere.
    """
    try:
        sync_module._local_storage["manifest"] = set()
        await sync_module.update_local_storage_manifest()
        return sync_module._local_storage.get("last_manifest_id")
    except Exception as exc:
        log.warning("Manifest-Refresh fehlgeschlagen: %s", exc)
        return sync_module._local_storage.get("last_manifest_id")


async def _delete_clip(sync_module, item, manifest_id) -> bool:
    """Löscht einen Clip mit der aktuellen manifest_id.

    max_retries=1, weil blinkpy sonst bis zu ~47 s pro Fehlversuch wartet.
    Wiederholungen mit frischem Manifest steuern wir selbst.
    """
    if manifest_id:
        item.url(manifest_id)  # aktualisiert item._manifest_id
    try:
        return await item.delete_video(_blink, max_retries=1)
    except Exception as exc:
        log.warning("Löschen von Clip %s fehlgeschlagen: %s", item.id, exc)
        return False


def _get_item(sync_name: str, clip_id: int):
    """Hilfsfunktion: Sync-Module und LocalStorageMediaItem anhand von IDs finden."""
    if _blink is None:
        raise HTTPException(status_code=401)
    sync_module = _blink.sync.get(sync_name)
    if sync_module is None:
        raise HTTPException(status_code=404, detail=f"Sync Module '{sync_name}' nicht gefunden")
    manifest = sync_module._local_storage.get("manifest", set())
    item = next((i for i in manifest if i.id == clip_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} nicht im Manifest")
    return sync_module, item


@app.delete("/api/video")
async def api_delete_video(clip_id: int, sync: str):
    """Einzelnen Clip vom Sync Module löschen."""
    global _video_cache, _last_refresh
    sync_module, item = _get_item(sync, clip_id)
    try:
        manifest_id = sync_module._local_storage.get("last_manifest_id")
        success = await _delete_clip(sync_module, item, manifest_id)

        if not success:
            # Manifest-ID war vermutlich veraltet -> neu laden und einmal wiederholen
            log.info("Clip %d: Löschen fehlgeschlagen, Manifest wird neu geladen …", clip_id)
            manifest_id = await _refresh_manifest(sync_module)
            manifest = sync_module._local_storage.get("manifest", set())
            item = next((i for i in manifest if i.id == clip_id), None)
            if item is None:
                # Clip ist nicht mehr im Manifest -> bereits gelöscht
                _video_cache = [v for v in _video_cache if v.get("_clip_id") != clip_id]
                log.info("Clip %d war bereits gelöscht.", clip_id)
                return JSONResponse({"ok": True})
            success = await _delete_clip(sync_module, item, manifest_id)

        if not success:
            raise HTTPException(status_code=502, detail="Löschen fehlgeschlagen")

        _video_cache = [v for v in _video_cache if v.get("_clip_id") != clip_id]
        sync_module._local_storage["manifest"].discard(item)
        log.info("Clip %d gelöscht.", clip_id)
        return JSONResponse({"ok": True})
    except HTTPException:
        raise
    except Exception as exc:
        log.error("Fehler beim Löschen von Clip %d: %s", clip_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


# Zustand des Hintergrund-Löschvorgangs
_delete_job: Dict = {
    "running": False, "total": 0, "deleted": 0, "failed": 0,
    "finished": False, "error": None,
}
_delete_task: Optional[asyncio.Task] = None


async def _delete_all_worker():
    """Löscht alle Clips im Hintergrund – mit Manifest-Refresh zwischen den Runden."""
    global _video_cache, _last_refresh, _delete_job

    try:
        for sync_name, sync_module in _blink.sync.items():
            # Bis zu 5 Runden: nach jeder Runde Manifest neu laden, da sich die
            # manifest_id durch die Löschvorgänge ändert.
            for round_no in range(5):
                manifest_id = await _refresh_manifest(sync_module)
                items = sorted(sync_module._local_storage.get("manifest", set()),
                               key=lambda i: i.id)
                if not items:
                    break

                if round_no == 0:
                    _delete_job["total"] = len(items)
                    log.info("Massenlöschen '%s': %d Clips.", sync_name, len(items))
                else:
                    log.info("Massenlöschen '%s': Runde %d, %d Clips verbleiben.",
                             sync_name, round_no + 1, len(items))
                    _delete_job["total"] = _delete_job["deleted"] + len(items)

                round_deleted = 0
                for item in items:
                    ok = await _delete_clip(sync_module, item, manifest_id)
                    if ok:
                        sync_module._local_storage["manifest"].discard(item)
                        _delete_job["deleted"] += 1
                        round_deleted += 1
                    else:
                        # manifest_id vermutlich veraltet -> Runde abbrechen,
                        # Manifest neu laden und mit dem Rest weitermachen
                        log.info("Clip %s: Löschen fehlgeschlagen – Manifest wird erneuert.",
                                 item.id)
                        break
                    await asyncio.sleep(0.4)  # Sync Module nicht überfahren

                if round_deleted == 0:
                    # Keine Fortschritte mehr -> abbrechen
                    _delete_job["failed"] = len(items)
                    log.warning("Massenlöschen '%s': keine Fortschritte mehr, %d Clips übrig.",
                                sync_name, len(items))
                    break

        _video_cache  = []
        _last_refresh = None
        log.info("Massenlöschen fertig: %d gelöscht, %d fehlgeschlagen.",
                 _delete_job["deleted"], _delete_job["failed"])

    except Exception as exc:
        log.error("Massenlöschen abgebrochen: %s", exc)
        _delete_job["error"] = str(exc)
        await handle_possible_auth_error(exc)
    finally:
        _delete_job["running"]  = False
        _delete_job["finished"] = True


@app.delete("/api/videos/all")
async def api_delete_all_videos():
    """Startet das Löschen aller Clips im Hintergrund und kehrt sofort zurück.

    Bei vielen Clips dauert der Vorgang mehrere Minuten – würde er innerhalb der
    HTTP-Anfrage laufen, liefe diese in den Timeout.
    """
    global _delete_task, _delete_job
    if _blink is None:
        raise HTTPException(status_code=401)

    if _delete_job["running"]:
        return JSONResponse({"ok": True, "already_running": True, **_delete_job})

    _delete_job = {
        "running": True, "total": 0, "deleted": 0, "failed": 0,
        "finished": False, "error": None,
    }
    _delete_task = asyncio.create_task(_delete_all_worker())
    return JSONResponse({"ok": True, "started": True})


@app.get("/api/videos/delete-status")
async def api_delete_status():
    """Fortschritt des Hintergrund-Löschvorgangs."""
    return JSONResponse(dict(_delete_job))


@app.get("/proxy/video")
async def proxy_video(path: str):
    return await _proxy_stream(path, "video/mp4")


@app.get("/proxy/thumb")
async def proxy_thumb(path: str):
    return await _proxy_stream(path, "image/jpeg")


def _stored_camera_thumb(name: str):
    """Liefert das zuletzt gespeicherte Kamera-Thumbnail von der Platte
    (ohne eine neue Aufnahme auszulösen). None, wenn keins vorhanden."""
    from fastapi.responses import Response
    safe_name = "".join(c if c.isalnum() else "_" for c in name)
    cache_file = THUMB_CACHE_DIR / f"{safe_name}.jpg"
    if cache_file.exists():
        return Response(
            content=cache_file.read_bytes(),
            media_type="image/jpeg",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )
    return None


@app.get("/proxy/thumb/camera/stored")
async def proxy_thumb_camera_stored(name: str):
    """Zuletzt gespeichertes Kamera-Thumbnail – löst KEINE neue Aufnahme aus.
    Wird als Fallback für Clip-Vorschaubilder verwendet."""
    result = _stored_camera_thumb(name)
    if result is None:
        raise HTTPException(status_code=404, detail="Kein Thumbnail verfügbar")
    return result


@app.get("/proxy/thumb/camera")
async def proxy_thumb_camera(name: str):
    """
    Liefert das aktuellste Vorschaubild dieser Kamera – identisch mit dem Bild,
    das im Dashboard auf der obersten Kachel steht (erster Frame des neuesten Clips).
    """
    return await thumb_latest(camera=name)


async def _extract_frame_ffmpeg(video_bytes: bytes) -> Optional[bytes]:
    """Extrahiert den ersten Frame eines Videos als JPEG via ffmpeg."""
    import shutil, asyncio.subprocess as asp

    # ffmpeg suchen: zuerst lokales Unterverzeichnis, dann PATH
    ffmpeg_candidates = [
        BASE_DIR / "ffmpeg" / "ffmpeg.exe",
        BASE_DIR / "ffmpeg" / "bin" / "ffmpeg.exe",  # typische ffmpeg-Zip-Struktur
        BASE_DIR / "ffmpeg" / "ffmpeg",
        BASE_DIR / "ffmpeg.exe",
        BASE_DIR / "ffmpeg",
    ]
    ffmpeg_bin = next((str(p) for p in ffmpeg_candidates if p.is_file()), None)
    if ffmpeg_bin is None:
        ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin is None:
        log.warning("ffmpeg nicht gefunden (gesucht in %s und PATH)", BASE_DIR / "ffmpeg")
        return None

    log.debug("Nutze ffmpeg: %s", ffmpeg_bin)
    try:
        proc = await asp.create_subprocess_exec(
            ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-vframes", "1", "-q:v", "4",
            "-f", "image2", "-vcodec", "mjpeg",
            "pipe:1",
            stdin=asp.PIPE, stdout=asp.PIPE, stderr=asp.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=video_bytes), timeout=30)
        if proc.returncode == 0 and stdout:
            log.debug("ffmpeg Thumbnail erstellt (%d bytes)", len(stdout))
            return stdout
        log.debug("ffmpeg stderr: %s", stderr.decode(errors="ignore")[:300])
    except Exception as exc:
        log.warning("ffmpeg Fehler: %s", exc)
    return None


@app.get("/proxy/thumb/clip")
async def proxy_thumb_clip(clip_id: int, sync: str):
    """
    Liefert ein echtes per-Clip-Vorschaubild (erster Frame).
    Beim ersten Aufruf: Clip laden + ffmpeg-Frame extrahieren, dann cachen.
    Fallback: Kamera-Thumbnail.
    """
    from fastapi.responses import Response

    # Cache-Datei prüfen
    cache_file = THUMB_CACHE_DIR / f"clip_{clip_id}.jpg"
    if cache_file.exists():
        return Response(content=cache_file.read_bytes(), media_type="image/jpeg")

    if _blink is None:
        raise HTTPException(status_code=401)

    sync_module = _blink.sync.get(sync)
    if sync_module is None:
        raise HTTPException(status_code=404)

    manifest = sync_module._local_storage.get("manifest", set())
    item = next((i for i in manifest if i.id == clip_id), None)
    if item is None:
        raise HTTPException(status_code=404)

    try:
        from blinkpy.api import http_get

        # Clip vorbereiten – Fehler hier nicht als fatal behandeln
        try:
            await item.prepare_download(_blink)
        except Exception as pe:
            log.warning("prepare_download für Clip %d: %s", clip_id, pe)

        manifest_id = sync_module._local_storage.get("last_manifest_id")
        url = _blink.urls.base_url + item.url(manifest_id)

        response = await http_get(_blink, url, stream=False, json=False)
        if response is None or response.status != 200:
            status = response.status if response else "None"
            log.warning("Clip %d: HTTP %s – Fallback auf Kamera-Thumbnail", clip_id, status)
            return RedirectResponse(f"/proxy/thumb/camera/stored?name={item.name}")

        video_bytes = await response.read()
        log.info("Clip %d geladen (%d bytes), extrahiere Frame …", clip_id, len(video_bytes))

        jpeg = await _extract_frame_ffmpeg(video_bytes)
        if jpeg:
            cache_file.write_bytes(jpeg)
            return Response(content=jpeg, media_type="image/jpeg")

    except Exception as exc:
        log.warning("Clip-Thumbnail Fehler für %d: %s – Fallback", clip_id, exc)

    # Fallback: Kamera-Thumbnail
    return RedirectResponse(f"/proxy/thumb/camera/stored?name={item.name}")


@app.get("/thumb/latest")
async def thumb_latest(camera: Optional[str] = None, no_cache: bool = True):
    """
    Liefert das Vorschaubild des zeitlich NEUESTEN Clips.
    Gedacht zum direkten Einbinden in externe Seiten, z.B.:
        <img src="http://<host>:9999/thumb/latest">
    Optional auf eine bestimmte Kamera einschränken: ?camera=Garten
    """
    from fastapi.responses import Response

    if _blink is None:
        log.warning("thumb_latest: Nicht eingeloggt (_blink ist None)")
        raise HTTPException(status_code=401, detail="Nicht eingeloggt")

    videos = await fetch_videos(days=30)
    log.info("thumb_latest: %d Videos gefunden (camera-Filter=%s)", len(videos), camera)

    if camera:
        videos = [v for v in videos if v.get("device_name") == camera]
        log.info("thumb_latest: nach Kamera-Filter '%s': %d Videos", camera, len(videos))

    if not videos:
        log.warning("thumb_latest: Keine Videos vorhanden – 404")
        raise HTTPException(status_code=404, detail="Kein Video gefunden")

    latest = videos[0]  # Liste ist bereits absteigend nach Datum sortiert
    thumb_url = latest.get("_thumb_url", "")
    log.info("thumb_latest: neuestes Video='%s' thumb_url='%s'", latest.get("device_name"), thumb_url)

    headers = {}
    if no_cache:
        # Verhindert, dass Browser/Proxys ein altes Bild zwischenspeichern
        headers["Cache-Control"] = "no-store, no-cache, must-revalidate"

    if not thumb_url:
        log.warning("thumb_latest: thumb_url ist leer – 404")
        raise HTTPException(status_code=404, detail="Kein Thumbnail verfügbar")

    # Internen Thumbnail-Endpunkt aufrufen und Bytes direkt zurückgeben
    # (kein Redirect, damit ein simples <img src="..."> ohne Redirect-Handling funktioniert)
    if "/proxy/thumb/clip" in thumb_url:
        import urllib.parse
        parsed = urllib.parse.urlparse(thumb_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        clip_id = int(params.get("clip_id", 0))
        sync    = params.get("sync", "")
        cache_file = THUMB_CACHE_DIR / f"clip_{clip_id}.jpg"
        if cache_file.exists():
            return Response(content=cache_file.read_bytes(), media_type="image/jpeg", headers=headers)
        # Noch nicht gecacht -> on-demand erzeugen
        result = await proxy_thumb_clip(clip_id=clip_id, sync=sync)
        if isinstance(result, Response):
            result.headers.update(headers)
            return result
        return result  # RedirectResponse-Fallback

    if "/proxy/thumb/camera" in thumb_url:
        import urllib.parse
        parsed = urllib.parse.urlparse(thumb_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        # Gespeichertes Bild verwenden – /thumb/latest soll keine Aufnahme auslösen
        result = _stored_camera_thumb(params.get("name", ""))
        if result is None:
            raise HTTPException(status_code=404, detail="Kein Thumbnail verfügbar")
        result.headers.update(headers)
        return result

    log.warning("thumb_latest: thumb_url '%s' matcht kein bekanntes Muster – 404", thumb_url)
    raise HTTPException(status_code=404, detail="Kein Thumbnail verfügbar")


@app.get("/proxy/download")
async def proxy_download(path: str, filename: str = "clip.mp4"):
    if _blink is None:
        raise HTTPException(status_code=401)
    from blinkpy.api import http_get
    base = _blink.urls.base_url if _blink.urls else "https://rest-prod.immedia-semi.com"
    url  = base + path if path.startswith("/") else path
    response = await http_get(_blink, url, stream=True, json=False)
    if response is None or response.status != 200:
        raise HTTPException(status_code=502)
    data = await response.read()
    return StreamingResponse(
        iter([data]),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print(f"  Blink Dashboard  →  http://localhost:{PORT}")
    print("=" * 55)
    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
