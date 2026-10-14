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
App Cloner. Handy für Handy, nie parallel. Kopien werden nie gelöscht.
"""

import logging
import time

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

    target = int(version["version_code"])
    cloner = Cloner(serial, progress)
    installed = phone_apps.read_phone(cloner.adb, serial)["packages"]

    def code(package):
        return installed.get(package, {}).get("version_code") or 0

    # Ganzes Handy = alle Kopien + Original (das soll für späteres Hand-Klonen mitziehen)
    if packages:
        wanted = [p for p in packages if p in installed]
    else:
        wanted = sorted(p for p in installed if is_clone(p)) + ([ORIGINAL] if ORIGINAL in installed else [])
    outdated = [p for p in wanted if code(p) < target]
    if not outdated:
        return {p: "schon aktuell" for p in wanted}
    # Der App Cloner klont aus dem Original — veraltete Kopien brauchen es zuerst
    original_due = code(ORIGINAL) < target and (
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
            # Ein unerwarteter Bildschirm kann den nächsten Schritt verfälschen — Handy aufgeben
            break
    return results


def run_job(client: SupabaseRest, job: dict) -> None:
    import phone_apps
    from device_monitor import _find_adb

    job_id = job["id"]

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
        _ensure_release(version)

        if job.get("serial"):
            serials = [job["serial"]]
        else:
            states = phone_apps._adb_devices(_find_adb())
            serials = sorted(s for s, state in states.items() if state == "device")
        packages = [job["package"]] if job.get("package") else None

        results, errors = {}, 0
        for index, serial in enumerate(serials, 1):
            if cancelled():
                break
            if not job.get("serial"):
                # Ganzer Mac: erst erledigen, was inzwischen einzeln angeklickt wurde
                while (narrow := _claim(client, max_scope=1)):
                    logger.info(f"IG-Auftrag #{narrow['id']} vorgezogen (Mac-Auftrag #{job_id} pausiert)")
                    progress(f"pausiert für Einzel-Auftrag #{narrow['id']}")
                    run_job(client, narrow)
            prefix = f"Handy {index}/{len(serials)} ({serial})" if len(serials) > 1 else serial

            def phone_progress(text, prefix=prefix):
                progress(f"{prefix}: {text}")

            try:
                results[serial] = update_phone(serial, version, packages, phone_progress, cancelled)
            except Exception as e:
                results[serial] = {"fehler": f"{type(e).__name__}: {e}"[:MAX_TEXT]}
            if any(str(v).startswith("Fehler") for v in results[serial].values()) or "fehler" in results[serial]:
                errors += 1
            try:
                report_inventory(client, [serial])
            except Exception as e:
                logger.warning(f"Bestand {serial} nicht gemeldet: {e}")

        if cancelled():
            _patch(client, job_id, {"result": results, "progress": "abgebrochen"})
            return
        _patch(client, job_id, {
            "status": "error" if errors else "done",
            "result": results,
            "progress": f"{len(serials) - errors} von {len(serials)} Handys ohne Fehler",
            "error": f"{errors} Handy(s) mit Fehler" if errors else None,
        })
    except Exception as e:
        logger.exception(f"IG-Auftrag #{job_id} gescheitert")
        _patch(client, job_id, {"status": "error", "error": f"{type(e).__name__}: {e}"[:MAX_TEXT]})


def run_ig_worker_job() -> dict:
    """Scheduler-Einstieg: ggf. Bestand melden, dann höchstens einen Auftrag abarbeiten."""
    global _last_inventory, _recovered
    client = _client()
    if client is None:
        return {"status": "no_supabase"}
    done = {}
    try:
        if not _recovered:
            done["recovered"] = _recover_orphans(client)
            _recovered = True
        if time.time() - _last_inventory > INVENTORY_MINUTES * 60:
            done["inventory"] = report_inventory(client)
            _last_inventory = time.time()
        job = _claim(client)
        if job:
            logger.info(f"IG-Auftrag #{job['id']}: {job.get('serial') or 'ganzer Mac'} / {job.get('package') or 'alle'}")
            run_job(client, job)
            done["job"] = job["id"]
    except Exception as e:
        logger.warning(f"IG-Worker: {e}")
        done["error"] = str(e)
    return done
