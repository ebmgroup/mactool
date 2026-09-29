"""Warn- und Crash-Archive des Bots: hochladen (7 Tage) und lokal aufräumen (60 Tage).

GramAddict legt bei jeder Warnung und jedem Crash ein ZIP ab
(`GramBotStorage/warnings/…zip`, `GramBotStorage/crashes/…zip`) mit Screenshot,
UI-Abbild, Log-Auszug und `reason.json` (Account, Zeit, Grund). Das Dashboard
(SK TEST 3) zeigt sie je Account an, samt Screenshot.

1. HOCHLADEN — nach jedem Sync. ZIPs der letzten UPLOAD_DAYS Tage, die noch nicht
   oben sind: ein verkleinertes JPEG (macOS `sips`) und eine JSON-Datei mit den
   Angaben aus `reason.json`, den Bildschirmtexten und dem Log-Ende. Ziel ist der
   eigene Bucket `bot-archiv` (privat), Pfad `<mac>/<JJJJ-MM-TT>/<art>-<name>.jpg|.json`.
2. BUCKET AUFRÄUMEN — einmal täglich: Datumsordner dieses Macs, die älter als
   UPLOAD_DAYS sind, werden gelöscht. Das Dashboard hält sich an dieselbe Frist.
3. LOKAL AUFRÄUMEN — einmal täglich: ZIPs älter als LOCAL_RETENTION_DAYS (60)
   werden auf dem Mac gelöscht (Andreas 29.09.2026: „es darf nie etwas neueres als
   60 Tage gelöscht werden"). Gelöscht wird nur, wenn ALLE Bedingungen stimmen:
     - Datei liegt direkt in `GramBotStorage/warnings` oder `GramBotStorage/crashes`,
     - Name hat genau das Muster des Bots `<version>_<JJJJ-MM-TT-hh-mm-ss>.zip`,
     - das Datum im Namen UND die Änderungszeit der Datei sind beide älter als 60 Tage,
     - die Uhr des Macs weicht höchstens einen Tag von der Uhr der Datenbank ab
       (ohne Serverzeit wird gar nicht gelöscht — lieber nichts als zu viel).
   Die Frist ist fest und lässt sich nicht unter 60 Tage stellen.

Alles hier ist „best effort": ein Fehler bricht den Sync nicht ab.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

from warnings_access import ARCHIV_ORDNER, screen_texts

logger = logging.getLogger(__name__)

BUCKET = "bot-archiv"
UPLOAD_DAYS = 7
LOCAL_RETENTION_DAYS = 60  # Untergrenze, siehe Modulkommentar — niemals kleiner.
MAX_UPLOADS_PER_RUN = 400
MAX_CLOCK_SKEW = timedelta(days=1)
JPEG_MAX_PX = 1000
JPEG_QUALITY = 60

# 3.7.9b0_2026-09-27-20-58-23.zip
NAME_RE = re.compile(r"^[0-9][0-9A-Za-z.]*_(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})\.zip$")

APP_DIR = Path(__file__).parent
_STATE_FILE = APP_DIR / "archive_state.json"


# ── Zustand ───────────────────────────────────────────────────────────


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return json.loads(_STATE_FILE.read_text())
    except Exception as e:
        logger.debug(f"Archiv-Zustand nicht lesbar: {e}")
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state))
    except Exception as e:
        logger.debug(f"Archiv-Zustand nicht schreibbar: {e}")


# ── Zeit ──────────────────────────────────────────────────────────────


def server_now(supabase_url: str, supabase_key: str, timeout: int = 15) -> datetime | None:
    """Uhrzeit der Datenbank aus dem Date-Header. None, wenn nicht erreichbar."""
    try:
        r = requests.get(
            f"{supabase_url.rstrip('/')}/rest/v1/",
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=timeout,
        )
        header = r.headers.get("Date")
        if not header:
            return None
        return parsedate_to_datetime(header).astimezone(timezone.utc)
    except Exception as e:
        logger.debug(f"Serverzeit nicht ermittelbar: {e}")
        return None


def name_datum(name: str) -> datetime | None:
    """Zeitpunkt aus dem Dateinamen des Bots (Ortszeit des Macs, ohne Zone)."""
    m = NAME_RE.match(name)
    if not m:
        return None
    try:
        return datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None


# ── Ordner ────────────────────────────────────────────────────────────


def archiv_ordner(storage_dir: Path) -> list[tuple[Path, str]]:
    """Nur die beiden festen Ordner direkt im Speicherordner — keine Suche."""
    out = []
    for ordner, art in ARCHIV_ORDNER.items():
        d = storage_dir / ordner
        if d.is_dir() and not d.is_symlink():
            out.append((d, art))
    return out


# ── 1. Hochladen ──────────────────────────────────────────────────────


def _jpeg(png: bytes) -> tuple[bytes, str]:
    """PNG mit `sips` verkleinern. Klappt das nicht, geht das Original hoch."""
    tmp = Path(tempfile.mkdtemp(prefix="botarchiv_"))
    try:
        src, dst = tmp / "s.png", tmp / "s.jpg"
        src.write_bytes(png)
        subprocess.run(
            ["sips", "-s", "format", "jpeg", "-s", "formatOptions", str(JPEG_QUALITY),
             "-Z", str(JPEG_MAX_PX), str(src), "--out", str(dst)],
            capture_output=True, timeout=60, check=True,
        )
        if dst.exists() and dst.stat().st_size > 0:
            return dst.read_bytes(), "image/jpeg"
    except Exception as e:
        logger.debug(f"sips fehlgeschlagen, lade PNG hoch: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return png, "image/png"


def archiv_eintrag(zip_path: Path, art: str, mac: str) -> tuple[dict, bytes | None]:
    """Die Angaben zu einem ZIP plus das PNG (falls vorhanden)."""
    meta: dict = {"mac": mac, "name": zip_path.name, "art": art}
    png = None
    with zipfile.ZipFile(zip_path) as z:
        names = [i.filename for i in z.infolist() if not i.is_dir()]
        if "reason.json" in names:
            try:
                meta["reason"] = json.loads(z.read("reason.json").decode("utf-8", "replace"))
            except Exception as e:
                meta["reason_fehler"] = str(e)[:200]
        xml = next((n for n in names if n.lower().endswith(".xml")), None)
        if xml:
            try:
                meta["screen"] = screen_texts(z.read(xml))[:120]
            except Exception as e:
                meta["screen_fehler"] = str(e)[:200]
        txt = "logs.txt" if "logs.txt" in names else next((n for n in names if n.endswith(".txt")), None)
        if txt:
            lines = z.read(txt).decode("utf-8", "replace").splitlines()
            meta["log_tail"] = [l[:400] for l in lines[-25:]]
        bild = next((n for n in names if n.lower().endswith(".png")), None)
        if bild:
            png = z.read(bild)
    return meta, png


def _upload(client, path: str, data: bytes, content_type: str) -> None:
    for versuch in (1, 2):
        try:
            client.storage.from_(BUCKET).upload(
                path, data, file_options={"content-type": content_type, "x-upsert": "true"}
            )
            return
        except Exception as e:
            if versuch == 1 and ("rate" in str(e).lower() or "timeout" in str(e).lower() or "429" in str(e)):
                time.sleep(10)
                continue
            raise


def _ensure_bucket(client) -> bool:
    try:
        client.storage.get_bucket(BUCKET)
        return True
    except Exception:
        pass
    try:
        client.storage.create_bucket(BUCKET, options={"public": False})
        logger.info(f"Bucket '{BUCKET}' angelegt")
        return True
    except Exception as e:
        msg = str(e).lower()
        if "already exists" in msg or "duplicate" in msg or "409" in msg:
            return True
        logger.error(f"Bucket '{BUCKET}' nicht verfügbar: {e}")
        return False


def upload_archive(client, mac: str, storage_dir: Path, now: datetime | None = None) -> dict:
    """Neue ZIPs der letzten UPLOAD_DAYS Tage hochladen."""
    result = {"status": "success", "uploaded": 0, "failed": 0, "skipped": 0}
    ordner = archiv_ordner(storage_dir)
    if not ordner:
        result["status"] = "no_dirs"
        return result
    if not _ensure_bucket(client):
        result["status"] = "error"
        return result

    now = now or datetime.now()
    grenze = now - timedelta(days=UPLOAD_DAYS)
    state = _load_state()
    oben: dict = state.get("hochgeladen", {})

    kandidaten = []
    for d, art in ordner:
        for p in d.glob("*.zip"):
            nd = name_datum(p.name)
            if nd is None or nd < grenze or f"{art}/{p.name}" in oben:
                continue
            kandidaten.append((nd, p, art))
    kandidaten.sort(reverse=True)  # neueste zuerst — die will man im Dashboard sehen

    for nd, p, art in kandidaten[:MAX_UPLOADS_PER_RUN]:
        try:
            meta, png = archiv_eintrag(p, art, mac)
            stamm = f"{mac}/{nd:%Y-%m-%d}/{art}-{p.stem}"
            if png:
                bild, ctype = _jpeg(png)
                ext = "jpg" if ctype == "image/jpeg" else "png"
                _upload(client, f"{stamm}.{ext}", bild, ctype)
                meta["bild"] = f"{stamm}.{ext}"
            _upload(client, f"{stamm}.json", json.dumps(meta, ensure_ascii=False).encode(), "application/json")
            oben[f"{art}/{p.name}"] = f"{nd:%Y-%m-%d}"
            result["uploaded"] += 1
            time.sleep(0.3)
        except Exception as e:
            result["failed"] += 1
            logger.warning(f"Archiv-Upload {p.name} fehlgeschlagen: {e}")
    result["skipped"] = max(0, len(kandidaten) - MAX_UPLOADS_PER_RUN)

    # Zustand klein halten: nur, was noch im Upload-Fenster liegt.
    halten = f"{(now - timedelta(days=UPLOAD_DAYS + 2)):%Y-%m-%d}"
    state["hochgeladen"] = {k: v for k, v in oben.items() if v >= halten}
    _save_state(state)
    if result["failed"] and not result["uploaded"]:
        result["status"] = "error"
    return result


# ── 2. Bucket aufräumen ───────────────────────────────────────────────


def cleanup_bucket(client, mac: str, now: datetime) -> int:
    """Datumsordner dieses Macs älter als UPLOAD_DAYS löschen (einmal täglich)."""
    state = _load_state()
    heute = f"{now:%Y-%m-%d}"
    if state.get("bucket_cleanup") == heute:
        return 0
    geloescht = 0
    grenze = f"{(now - timedelta(days=UPLOAD_DAYS)):%Y-%m-%d}"
    try:
        for f in client.storage.from_(BUCKET).list(mac, {"limit": 1000}) or []:
            tag = f.get("name", "")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", tag) or tag >= grenze:
                continue
            for _runde in range(100):  # Obergrenze: nie endlos, falls Löschen still scheitert
                dateien = client.storage.from_(BUCKET).list(f"{mac}/{tag}", {"limit": 1000}) or []
                pfade = [f"{mac}/{tag}/{x['name']}" for x in dateien if x.get("name")]
                if not pfade:
                    break
                client.storage.from_(BUCKET).remove(pfade)
                geloescht += len(pfade)
        state["bucket_cleanup"] = heute
        _save_state(state)
    except Exception as e:
        logger.warning(f"Archiv-Bucket aufräumen fehlgeschlagen (nicht schlimm): {e}")
    return geloescht


# ── 3. Lokal aufräumen ────────────────────────────────────────────────


def lokal_zu_loeschen(storage_dir: Path, now_local: datetime) -> list[Path]:
    """Welche ZIPs gelöscht werden dürfen — nach ALLEN Regeln im Modulkommentar."""
    grenze = now_local - timedelta(days=LOCAL_RETENTION_DAYS)
    out = []
    for d, _art in archiv_ordner(storage_dir):
        for p in d.iterdir():
            if p.is_symlink() or not p.is_file() or p.parent != d:
                continue
            nd = name_datum(p.name)
            if nd is None or nd >= grenze:
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
            except OSError:
                continue
            if mtime >= grenze:
                continue
            out.append(p)
    return out


def cleanup_local(storage_dir: Path, server_time: datetime | None, ausfuehren: bool = True,
                  now_local: datetime | None = None) -> dict:
    """ZIPs älter als 60 Tage auf dem Mac löschen. Ohne verlässliche Uhr: nichts."""
    now_local = now_local or datetime.now()
    result = {"status": "success", "geloescht": 0, "kandidaten": 0, "frist_tage": LOCAL_RETENTION_DAYS}
    if server_time is None:
        result["status"] = "skipped_no_server_time"
        return result
    lokal_utc = now_local.astimezone(timezone.utc)
    abweichung = abs(lokal_utc - server_time)
    result["uhr_abweichung_s"] = int(abweichung.total_seconds())
    if abweichung > MAX_CLOCK_SKEW:
        result["status"] = "skipped_clock_skew"
        logger.warning(f"Archiv lokal NICHT aufgeräumt: Mac-Uhr weicht {abweichung} von der Datenbank ab")
        return result
    liste = lokal_zu_loeschen(storage_dir, now_local)
    result["kandidaten"] = len(liste)
    if liste:
        result["aeltester"] = min(p.name for p in liste)
        result["juengster"] = max(p.name for p in liste)
    if not ausfuehren:
        result["status"] = "dry_run"
        return result
    for p in liste:
        try:
            p.unlink()
            result["geloescht"] += 1
        except OSError as e:
            logger.debug(f"{p.name} nicht löschbar: {e}")
    logger.info(f"Archiv lokal: {result['geloescht']} ZIPs älter als {LOCAL_RETENTION_DAYS} Tage gelöscht")
    return result


def run_daily_local_cleanup(storage_dir: Path, server_time: datetime | None) -> dict | None:
    """Höchstens einmal am Tag."""
    state = _load_state()
    heute = f"{datetime.now():%Y-%m-%d}"
    if state.get("lokal_cleanup") == heute:
        return None
    r = cleanup_local(storage_dir, server_time)
    if r["status"] == "success":
        state = _load_state()
        state["lokal_cleanup"] = heute
        _save_state(state)
    return r


def run_archive(client, mac: str, storage_dir: Path, supabase_url: str, supabase_key: str) -> dict:
    """Alles nach einem Sync: hochladen, Bucket und Mac aufräumen."""
    out: dict = {}
    try:
        out["upload"] = upload_archive(client, mac, storage_dir)
    except Exception as e:
        out["upload"] = {"status": "error", "error": str(e)[:300]}
    srv = server_now(supabase_url, supabase_key)
    try:
        out["bucket_geloescht"] = cleanup_bucket(client, mac, (srv or datetime.now(timezone.utc)).replace(tzinfo=None))
    except Exception as e:
        out["bucket_geloescht"] = f"Fehler: {e}"[:300]
    try:
        out["lokal"] = run_daily_local_cleanup(storage_dir, srv)
    except Exception as e:
        out["lokal"] = {"status": "error", "error": str(e)[:300]}
    return out
