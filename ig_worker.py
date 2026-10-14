"""IG Versionen: Handy-Bestand melden und Update-Aufträge aus dem Dashboard abarbeiten.

Läuft als eigener Scheduler-Job, nicht über die Befehlsqueue: ein ganzer Mac dauert
Stunden (pro Kopie ~6 min, pro Handy bis 8 Kopien), und in der Zeit müssen
Fernbefehle wie `status` weiter beantwortet werden.

Tabellen (tools/schema_ig.sql, Kontroll-Supabase):
  ig_versions     — bereitgestellte Versionen, eine davon Standard
  ig_phones       — Bestand je Handy, schreibt nur der Mac
  ig_update_jobs  — Aufträge; serial null = ganzer Mac, package null = ganzes Handy

Ablauf je Handy: warten, bis der Bot Instagram nicht im Vordergrund hat; Original auf
die Version heben (der App Cloner klont aus dem Original); dann jede Kopie per
App Cloner. Bis zu `ig_parallel_phones` Phones gleichzeitig, nie zweimal dasselbe.
"Mac updaten" stoppt den Bot und startet ihn danach wieder. Kopien werden nie gelöscht.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import get_config
from supabase_rest import SupabaseRest

logger = logging.getLogger(__name__)

ORIGINAL = "com.instagram.android"
INVENTORY_MINUTES = 30
BUSY_WAIT_SECONDS = 20 * 60   # so lange auf einen freien Moment warten, dann Handy überspringen
MAX_TEXT = 500

_last_inventory = 0.0
_recovered = False


def _client() -> SupabaseRest | None:
    cfg = get_config()
    if not (cfg.supabase_url and cfg.supabase_key):
        return None
    return SupabaseRest(cfg.supabase_url, cfg.supabase_key)


# ── Bestand ───────────────────────────────────────────────────────────


def _phone_row(server: str, phone: dict) -> dict:
    keep = ("version_name", "version_code", "abi", "signature", "last_update", "first_install",
            "system", "updated_system", "apk_count")
    return {
        "server_name": server,
        "serial": phone["serial"],
        "name": phone.get("name"),
        "state": phone.get("state"),
        "manufacturer": phone.get("manufacturer"),
        "model": phone.get("model"),
        "android": phone.get("android"),
        "locale": phone.get("locale"),
        "error": (phone.get("error") or None) and phone["error"][:MAX_TEXT],
        "packages": {pkg: {k: info.get(k) for k in keep} for pkg, info in (phone.get("packages") or {}).items()},
    }


def report_inventory(client: SupabaseRest, serials: list[str] | None = None) -> int:
    """Bestand lesen und nach ig_phones schreiben. Ohne serials: alle + Verschwundene löschen."""
    import phone_apps

    server = get_config().server_name
    result = phone_apps.inventory(serials)
    phones = result.get("phones", [])
    if not phones and result.get("verdict") == "adb nicht gefunden":
        return 0
    client.upsert_many("ig_phones", [_phone_row(server, p) for p in phones], on_conflict="server_name,serial")
    if serials is None:
        seen = {p["serial"] for p in phones}
        stored = client.select("ig_phones", {"select": "serial", "server_name": f"eq.{server}"})
        gone = [row["serial"] for row in stored if row["serial"] not in seen]
        if gone:
            client.delete("ig_phones", {"server_name": f"eq.{server}",
                                        "serial": f"in.({','.join(gone)})"})
    return len(phones)


# ── Aufträge ──────────────────────────────────────────────────────────


def _patch(client: SupabaseRest, job_id: int, values: dict) -> None:
    try:
        client.update("ig_update_jobs", {"id": f"eq.{job_id}"}, values)
    except Exception as e:
        logger.warning(f"ig_update_jobs #{job_id} nicht aktualisiert: {e}")


def _cancelled(client: SupabaseRest, job_id: int) -> bool:
    rows = client.select("ig_update_jobs", {"select": "status", "id": f"eq.{job_id}"})
    return bool(rows) and rows[0]["status"] == "cancelled"


def _recover_orphans(client: SupabaseRest) -> int:
    """Nach einem Neustart: "running"-Aufträge dieses Macs gehören niemandem mehr.

    Ein Update des MacTools (oder ein Absturz) beendet den Prozess mitten im Auftrag.
    Wieder einreihen ist sicher: was schon auf Stand ist, wird übersprungen.
    """
    server = get_config().server_name
    rows = client.update("ig_update_jobs", {"server_name": f"eq.{server}", "status": "eq.running"},
                         {"status": "queued", "progress": "nach Neustart des MacTools erneut eingereiht"})
    return len(rows)


def _claim(client: SupabaseRest, max_scope: int = 2) -> dict | None:
    """Nächsten Auftrag reservieren: schmale zuerst (App, dann Phone, dann Mac), sonst nach Zeit.

    max_scope=1 holt nur App-/Phone-Aufträge — so arbeitet ein laufender Mac-Auftrag
    zwischendurch ab, was Mitarbeiter einzeln angeklickt haben.
    """
    server = get_config().server_name
    rows = client.select("ig_update_jobs", {
        "select": "*", "server_name": f"eq.{server}", "status": "eq.queued",
        "scope": f"lte.{max_scope}", "order": "scope.asc,created_at.asc", "limit": "1",
    })
    if not rows:
        return None
    won = client.update("ig_update_jobs", {"id": f"eq.{rows[0]['id']}", "status": "eq.queued"},
                        {"status": "running", "progress": "gestartet"}, retries=1)
    return won[0] if won else None


def _version(client: SupabaseRest, version_id: int | None) -> dict:
    params = {"select": "*", "status": "eq.ready"}
    params.update({"id": f"eq.{version_id}"} if version_id else {"is_standard": "is.true"})
    rows = client.select("ig_versions", params)
    if not rows:
        raise RuntimeError("Keine (Standard-)Version bereit")
    return rows[0]


def _ensure_release(version: dict) -> dict:
    """Release lokal vorhanden und unverändert? Sonst holen."""
    import json

    import apk_rollout

    meta_file = apk_rollout._release_dir(version["release"]) / "release.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
        if meta.get("sha256") == version.get("sha256") and all(
            (meta_file.parent / name).exists() for name in meta.get("apks", [])
        ):
            return meta
    return apk_rollout.fetch(version["release"])


def _wait_until_idle(cloner, progress) -> bool:
    """Der Bot bedient Instagram über die Oberfläche — nicht dazwischenfunken."""
    deadline = time.time() + BUSY_WAIT_SECONDS
    while True:
        focus = cloner.foreground()
        if "com.instagram." not in focus:
            return True
        if time.time() > deadline:
            return False
        progress("wartet, bis der Bot Instagram verlässt")
        time.sleep(60)


def update_phone(serial: str, version: dict, packages: list[str] | None, progress, cancelled) -> dict:
    """Ein Handy: Original, dann die gewünschten Kopien. Liefert Ergebnis je Paket."""
    import apk_rollout
    import phone_apps
    from app_cloner import Cloner, is_clone

    from app_cloner import version_key

    target = version["version_name"]
    cloner = Cloner(serial, progress)
    installed = phone_apps.read_phone(cloner.adb, serial)["packages"]

    def key(package):
        return version_key(installed.get(package, {}).get("version_name"))

    # Ganzes Handy = alle Kopien + Original (das soll für späteres Hand-Klonen mitziehen)
    if packages:
        wanted = [p for p in packages if p in installed]
    else:
        wanted = sorted(p for p in installed if is_clone(p)) + ([ORIGINAL] if ORIGINAL in installed else [])
    # Veraltet = älterer Versionsname. Neuere bleiben unangetastet (wie im Dashboard grau).
    outdated = [p for p in wanted if key(p) < version_key(target)]
    if not outdated:
        return {p: "schon aktuell" for p in wanted}
    # Der App Cloner klont aus dem Original — es muss GENAU auf dem Standard stehen,
    # auch wenn es neuer ist (sonst würden die Kopien z. B. 450 statt 445).
    original_due = key(ORIGINAL) != version_key(target) and (
        ORIGINAL in outdated or any(is_clone(p) for p in outdated))

    if not _wait_until_idle(cloner, progress):
        raise apk_rollout.RolloutError("Bot hatte Instagram 20 Minuten lang im Vordergrund — Handy übersprungen")

    results = {}
    if original_due:
        progress("Original aktualisieren")
        result = apk_rollout.install_original(serial, version["release"])
        results[ORIGINAL] = result["verdict"]
        if not result["verdict"].startswith("OK"):
            raise apk_rollout.RolloutError(f"Original: {result['verdict']}")

    for package in outdated:
        if package == ORIGINAL:
            continue
        if cancelled():
            results[package] = "abgebrochen"
            break
        try:
            results[package] = cloner.update_clone(package, target)["result"]
        except apk_rollout.RolloutError as e:
            results[package] = f"Fehler: {e}"
            # Sofort sichtbar machen — das Ergebnis landet sonst erst am Auftragsende in der DB
            logger.warning(f"[{serial}] {package}: {e}")
            progress(f"Fehler bei {package}: {e}")
            # Ein unerwarteter Bildschirm kann den nächsten Schritt verfälschen — Handy aufgeben
            break
    return results


# ── Parallelbetrieb ───────────────────────────────────────────────────
#
# Der Scheduler-Job (jede Minute) ist nur der Verteiler: er nimmt Aufträge an und startet
# sie in eigenen Threads. Grenzen je Mac:
#   - höchstens `ig_parallel_phones` Phones gleichzeitig (Semaphore, alle Aufträge zusammen)
#   - nie zwei Aufträge auf demselben Phone (Lock je Seriennummer)
#   - höchstens ein Mac-Auftrag gleichzeitig
# "Mac updaten" stoppt den Bot für die Dauer (Andreas, 14.10.); App-/Phone-Klicks nicht —
# da kümmert sich der Mitarbeiter.

_lock = threading.Lock()
_slots: threading.BoundedSemaphore | None = None
_slot_count = 0
_phone_locks: dict[str, threading.Lock] = {}
_active: dict[int, int] = {}          # job_id -> scope
_release_lock = threading.Lock()

PAUSE_FILE = Path(__file__).parent / "ig_bot_pause.json"


def _parallel() -> int:
    return max(1, min(10, int(getattr(get_config(), "ig_parallel_phones", 5) or 5)))


def _phone_slot():
    """Semaphore für alle Phone-Arbeiten dieses Macs (Größe folgt der Einstellung)."""
    global _slots, _slot_count
    with _lock:
        if _slots is None or (_slot_count != _parallel() and not _active):
            _slot_count = _parallel()
            _slots = threading.BoundedSemaphore(_slot_count)
        return _slots


def _phone_lock(serial: str) -> threading.Lock:
    with _lock:
        return _phone_locks.setdefault(serial, threading.Lock())


def _run_phone(client, job_id, serial, version, packages, progress, cancelled) -> dict:
    """Ein Phone unter den Parallel-Grenzen bearbeiten, danach Bestand melden."""
    slot = _phone_slot()
    progress("wartet auf freien Platz")
    with slot, _phone_lock(serial):
        if cancelled():
            return {"abgebrochen": "vor dem Start"}
        try:
            result = update_phone(serial, version, packages, progress, cancelled)
        except Exception as e:
            result = {"fehler": f"{type(e).__name__}: {e}"[:MAX_TEXT]}
        try:
            report_inventory(client, [serial])
        except Exception as e:
            logger.warning(f"Bestand {serial} nicht gemeldet: {e}")
        return result


# ── Bot-Pause für "Mac updaten" ───────────────────────────────────────


def _pause_bot(job_id: int) -> bool:
    """Bot stoppen und merken, ob er vorher lief — nur dann wird er danach wieder gestartet."""
    import bot_manager

    was_running = bot_manager.is_bot_running()
    PAUSE_FILE.write_text(json.dumps({"job": job_id, "was_running": was_running, "since": time.time()}))
    if was_running:
        logger.info(f"IG-Auftrag #{job_id}: Bot wird für das Mac-Update gestoppt")
        bot_manager.stop_bot()
    return was_running


def _resume_bot() -> bool:
    """Bot wieder starten, falls WIR ihn gestoppt haben. Idempotent."""
    import bot_manager

    if not PAUSE_FILE.exists():
        return False
    try:
        was_running = json.loads(PAUSE_FILE.read_text()).get("was_running", True)
    except Exception:
        was_running = True   # im Zweifel lieber wieder starten
    if was_running:
        logger.info("IG: Bot nach dem Mac-Update wieder starten")
        if not bot_manager.start_bot():
            # Datei bleibt liegen -> nächster Durchlauf versucht es erneut
            logger.error("IG: Bot ließ sich nicht starten — neuer Versuch in einer Minute")
            return False
    PAUSE_FILE.unlink(missing_ok=True)
    return True


def bot_paused_since() -> float | None:
    try:
        return json.loads(PAUSE_FILE.read_text()).get("since")
    except Exception:
        return None


# ── Auftrag ───────────────────────────────────────────────────────────


def run_job(client: SupabaseRest, job: dict) -> None:
    import phone_apps
    from device_monitor import _find_adb

    job_id = job["id"]
    whole_mac = not job.get("serial")

    def progress(text: str):
        _patch(client, job_id, {"progress": text[:MAX_TEXT]})

    def cancelled() -> bool:
        return _cancelled(client, job_id)

    try:
        # Der Standard beim START zählt, nicht der beim Klick: hat ein Admin inzwischen
        # umgestellt, soll kein wartender Auftrag noch die alte Version ausrollen.
        version = _version(client, None)
        if version["id"] != job.get("version_id"):
            _patch(client, job_id, {"version_id": version["id"]})
        progress(f"Version {version['version_name']} holen")
        with _release_lock:
            _ensure_release(version)

        packages = [job["package"]] if job.get("package") else None
        if not whole_mac:
            results = {job["serial"]: _run_phone(client, job_id, job["serial"], version, packages,
                                                 progress, cancelled)}
        else:
            states = phone_apps._adb_devices(_find_adb())
            serials = sorted(s for s, state in states.items() if state == "device")
            _pause_bot(job_id)
            done = []
            try:
                def one(serial):
                    def phone_progress(text):
                        progress(f"Bot pausiert · {len(done)}/{len(serials)} Phones fertig · {serial}: {text}")
                    result = _run_phone(client, job_id, serial, version, None, phone_progress, cancelled)
                    done.append(serial)
                    return serial, result

                with ThreadPoolExecutor(max_workers=_parallel()) as pool:
                    results = dict(pool.map(one, serials))
            finally:
                _resume_bot()

        errors = sum(1 for r in results.values()
                     if "fehler" in r or any(str(v).startswith("Fehler") for v in r.values()))
        if cancelled():
            _patch(client, job_id, {"result": results, "progress": "abgebrochen"})
            return
        _patch(client, job_id, {
            "status": "error" if errors else "done",
            "result": results,
            "progress": f"{len(results) - errors} von {len(results)} Phones ohne Fehler"
                        + (" · Bot wieder gestartet" if whole_mac else ""),
            "error": f"{errors} Phone(s) mit Fehler" if errors else None,
        })
    except Exception as e:
        logger.exception(f"IG-Auftrag #{job_id} gescheitert")
        _patch(client, job_id, {"status": "error", "error": f"{type(e).__name__}: {e}"[:MAX_TEXT]})
    finally:
        with _lock:
            _active.pop(job_id, None)


def _start(job: dict) -> None:
    """Auftrag im eigenen Thread mit eigenem Client starten."""
    with _lock:
        _active[job["id"]] = job.get("scope", 2 if not job.get("serial") else 1)
    logger.info(f"IG-Auftrag #{job['id']}: {job.get('serial') or 'ganzer Mac'} / {job.get('package') or 'alle'}")
    threading.Thread(target=run_job, args=(_client(), job), name=f"ig-job-{job['id']}", daemon=True).start()


def dispatch(client: SupabaseRest) -> list[int]:
    """So viele wartende Aufträge annehmen, wie Platz ist. Liefert die gestarteten IDs.

    Einzel-Aufträge (App/Phone) bis zur Parallelgrenze, Mac-Aufträge höchstens einer.
    Schmale zuerst (_claim sortiert nach scope). Alle teilen sich die Phone-Plätze.
    """
    started = []
    while True:
        with _lock:
            singles = sum(1 for scope in _active.values() if scope < 2)
            mac_running = any(scope == 2 for scope in _active.values())
        if singles < _parallel():
            job = _claim(client, max_scope=1 if mac_running else 2)
        elif not mac_running:
            job = _claim_scope(client, 2)
        else:
            job = None
        if not job:
            return started
        _start(job)
        started.append(job["id"])


def _claim_scope(client: SupabaseRest, scope: int) -> dict | None:
    server = get_config().server_name
    rows = client.select("ig_update_jobs", {
        "select": "*", "server_name": f"eq.{server}", "status": "eq.queued",
        "scope": f"eq.{scope}", "order": "created_at.asc", "limit": "1",
    })
    if not rows:
        return None
    won = client.update("ig_update_jobs", {"id": f"eq.{rows[0]['id']}", "status": "eq.queued"},
                        {"status": "running", "progress": "gestartet"}, retries=1)
    return won[0] if won else None


def run_ig_worker_job() -> dict:
    """Scheduler-Einstieg (jede Minute): aufräumen, ggf. Bestand melden, Aufträge verteilen."""
    global _last_inventory, _recovered
    client = _client()
    if client is None:
        return {"status": "no_supabase"}
    done = {}
    try:
        if not _recovered:
            done["recovered"] = _recover_orphans(client)
            _recovered = True
        with _lock:
            mac_running = any(scope == 2 for scope in _active.values())
            idle = not _active
        # Bot noch von uns gestoppt, aber kein Mac-Auftrag mehr aktiv (Absturz, Neustart,
        # Abbruch)? Dann wieder starten — der Bot darf nie liegen bleiben.
        if PAUSE_FILE.exists() and not mac_running:
            server = get_config().server_name
            open_mac = client.select("ig_update_jobs", {
                "select": "id", "server_name": f"eq.{server}", "status": "in.(queued,running)",
                "scope": "eq.2", "limit": "1",
            })
            if not open_mac:
                done["bot_resumed"] = _resume_bot()
        if idle and time.time() - _last_inventory > INVENTORY_MINUTES * 60:
            done["inventory"] = report_inventory(client)
            _last_inventory = time.time()
        done["started"] = dispatch(client)
    except Exception as e:
        logger.warning(f"IG-Worker: {e}")
        done["error"] = str(e)
    return done
