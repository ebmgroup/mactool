"""Was der Fernzugriff auf diesem Mac ausführen darf.

Eine feste Liste, kein `eval`, kein Shell. Was hier nicht steht, passiert nicht —
auch dann nicht, wenn jemand mit dem Supabase-Key eine beliebige Zeile in die
Befehlstabelle schreibt.

Die Handler sind absichtlich dünn: sie rufen denselben Code auf, den das Dashboard
schon benutzt. So kann die Ferndiagnose nicht etwas anderes tun als der Knopf.
"""

import logging
import re
from datetime import datetime
from pathlib import Path

from config import SECRET_FIELDS, get_config, masked_config_dict

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent

# Antworten wandern als jsonb durch Postgres und über die Queue zurück. Ein
# ungebremster Log-Tail wären Megabytes pro Abruf.
MAX_LOG_LINES = 2000
DEFAULT_LOG_LINES = 200
MAX_LINE_CHARS = 500
MAX_LISTED_FILES = 200


class CommandError(RuntimeError):
    """Der Befehl ist so nicht ausführbar (falscher Name, falsche Argumente)."""


# ── Lesende Befehle ───────────────────────────────────────────────────


def _cmd_status(args: dict) -> dict:
    """Zustand des Macs. Ohne GitHub-Abfrage — die kostet bis zu 15 s."""
    from status import build_status

    return build_status(check_updates=bool(args.get("check_updates", False)))


def _cmd_config(args: dict) -> dict:
    """Die komplette Konfiguration, Geheimnisse maskiert."""
    return masked_config_dict()


def _resolve_log_file(name: str | None) -> Path:
    """Logdatei auflösen — ausschließlich aus dem logs-Verzeichnis des Tools.

    Der Name wird auf seinen Dateianteil reduziert, bevor er benutzt wird. Ohne
    das wäre `file: "../../../etc/passwd"` ein Leseweg auf beliebige Dateien.
    """
    log_dir = (APP_DIR / "logs").resolve()
    candidate = (log_dir / Path(name or "mactool.log").name).resolve()
    if candidate.parent != log_dir:
        raise CommandError(f"Logdatei außerhalb von {log_dir} ist nicht erlaubt")
    return candidate


def _cmd_logs(args: dict) -> dict:
    """Das Ende von logs/mactool.log, optional nach einem Muster gefiltert."""
    log_file = _resolve_log_file(args.get("file"))
    lines = min(int(args.get("lines") or DEFAULT_LOG_LINES), MAX_LOG_LINES)
    pattern = args.get("grep")

    if not log_file.exists():
        return {"file": str(log_file), "exists": False, "lines": []}

    try:
        matcher = re.compile(pattern, re.IGNORECASE) if pattern else None
    except re.error as e:
        raise CommandError(f"Ungültiges Suchmuster '{pattern}': {e}")

    # Zeilenweise lesen statt readlines(): mactool.log darf 5 MB groß werden
    # und der Filter wirft das meiste ohnehin weg.
    from collections import deque

    tail: deque = deque(maxlen=lines)
    scanned = 0
    with open(log_file, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            scanned += 1
            if matcher and not matcher.search(line):
                continue
            tail.append(line.rstrip("\n")[:MAX_LINE_CHARS])

    return {
        "file": str(log_file),
        "exists": True,
        "bytes": log_file.stat().st_size,
        "scanned_lines": scanned,
        "grep": pattern,
        "returned": len(tail),
        "lines": list(tail),
    }


def _cmd_files(args: dict) -> dict:
    """Verzeichnisinhalt — entweder die Bot-Logs oder die des Tools."""
    which = (args.get("dir") or "botlogs").lower()

    if which == "botlogs":
        directory = Path(get_config().sqlite_db_path).expanduser().parent / "logs"
    elif which == "mactool":
        directory = APP_DIR / "logs"
    else:
        raise CommandError(f"Unbekanntes Verzeichnis '{which}' (erlaubt: botlogs, mactool)")

    if not directory.exists():
        return {"dir": str(directory), "exists": False, "files": []}

    entries = []
    for path in sorted(directory.iterdir())[:MAX_LISTED_FILES]:
        try:
            info = path.stat()
            entries.append(
                {
                    "name": path.name,
                    "bytes": info.st_size,
                    "modified": datetime.fromtimestamp(info.st_mtime).isoformat(
                        timespec="seconds"
                    ),
                }
            )
        except Exception as e:
            entries.append({"name": path.name, "error": f"{type(e).__name__}: {e}"})

    total = sum(1 for _ in directory.iterdir())
    return {
        "dir": str(directory),
        "exists": True,
        "total_files": total,
        "listed": len(entries),
        "truncated": total > len(entries),
        "files": entries,
    }


def _cmd_diag_upload(args: dict) -> dict:
    import diagnostics

    return diagnostics.diagnose_log_upload()


def _cmd_diag_db(args: dict) -> dict:
    import diagnostics

    return diagnostics.diagnose_database()


def _cmd_versions(args: dict) -> dict:
    from updater import check_for_updates, get_available_versions, get_current_version

    return {
        "current": get_current_version(),
        "available": get_available_versions(),
        "check": check_for_updates(),
    }


def _cmd_legacy_upload(args: dict) -> dict:
    """Status der alten upload-macXX.py-LaunchAgents."""
    import legacy_upload

    return legacy_upload.get_status()


# ── Eingreifende Befehle (brauchen remote_allow_actions) ──────────────


def _cmd_sync(args: dict) -> dict:
    from sync import trigger_sync

    upload_all = args.get("upload_all")
    return trigger_sync(upload_all_logs=True if upload_all is None else bool(upload_all))


# Diese Felder lassen sich aus der Ferne NICHT setzen.
#   server_name — der Mac holt seine Aufträge über genau diesen Namen ab. Wer ihn
#                 ändert, schneidet den Mac von der Fernsteuerung ab; zurück ginge
#                 es nur noch per RustDesk.
#   SECRET_FIELDS — Keys und Passwörter bleiben dem Dashboard vorbehalten.
UNSETTABLE = frozenset({"server_name"}) | SECRET_FIELDS

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _coerce(name: str, value, field_type):
    """Einen übergebenen Wert auf den Typ des Konfigurationsfeldes bringen."""
    if field_type is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "ja", "an", "yes", "on"):
            return True
        if text in ("false", "0", "nein", "aus", "no", "off"):
            return False
        raise CommandError(f"'{name}' erwartet ja/nein, bekam {value!r}")

    if field_type is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise CommandError(f"'{name}' erwartet eine Zahl, bekam {value!r}")

    if field_type == list[str]:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, list):
            raise CommandError(f"'{name}' erwartet eine Liste, bekam {value!r}")
        return [str(item) for item in value]

    return str(value)


def _validate(name: str, value):
    """Werte prüfen, deren Unsinn erst Stunden später auffiele."""
    if name == "sync_times":
        if not value:
            raise CommandError("sync_times darf nicht leer sein — sonst läuft kein Upload mehr")
        bad = [t for t in value if not TIME_RE.match(t)]
        if bad:
            raise CommandError(f"Ungültige Uhrzeit(en) in sync_times: {', '.join(bad)} (Format HH:MM)")

    if name == "remote_poll_seconds" and value < 5:
        raise CommandError("remote_poll_seconds unter 5 ist nur Last ohne Nutzen")

    if name == "customer_stats_source" and value not in ("sessions", "superdb"):
        raise CommandError("customer_stats_source muss 'sessions' oder 'superdb' sein")

    if name == "alert_smtp_security" and value not in ("starttls", "ssl", "none"):
        raise CommandError("alert_smtp_security muss 'starttls', 'ssl' oder 'none' sein")

    if name == "alert_smtp_port" and not 1 <= value <= 65535:
        raise CommandError("alert_smtp_port muss zwischen 1 und 65535 liegen")


def _cmd_set(args: dict) -> dict:
    """Konfigurationsfelder aus der Ferne setzen.

    Keys und Passwörter sind bewusst ausgenommen — die gehören ins Dashboard.
    Ebenso `server_name`: den zu ändern hieße, den Mac von der Fernsteuerung
    abzuschneiden.
    """
    from dataclasses import fields as dataclass_fields

    from config import AppConfig

    if not args:
        raise CommandError("Nichts zu setzen — erwartet wird feld=wert")

    types = {f.name: f.type for f in dataclass_fields(AppConfig)}
    config = get_config()

    unknown = sorted(set(args) - set(types))
    if unknown:
        raise CommandError(
            f"Unbekannte Felder: {', '.join(unknown)}. "
            f"Bekannt sind: {', '.join(sorted(types))}"
        )

    blocked = sorted(set(args) & UNSETTABLE)
    if blocked:
        raise CommandError(
            f"Aus der Ferne nicht setzbar: {', '.join(blocked)}. "
            f"Keys, Passwörter und der Servername gehören ins Dashboard."
        )

    changed = {}
    for name, raw in args.items():
        value = _coerce(name, raw, types[name])
        _validate(name, value)
        before = getattr(config, name)
        if before == value:
            continue
        setattr(config, name, value)
        changed[name] = {"vorher": before, "nachher": value}

    if not changed:
        return {"changed": {}, "note": "Alle Werte standen bereits so"}

    config.save()
    logger.info(f"Fernzugriff hat Konfiguration geändert: {sorted(changed)}")

    result = {"changed": changed}

    # sync_times wirkte bisher erst nach einem Neustart — jetzt sofort
    if "sync_times" in changed:
        from scheduler import get_scheduler

        result["sync_jobs"] = get_scheduler().reload_sync_jobs()

    # Diese wirken erst beim nächsten Start des Dienstes
    needs_restart = sorted(
        name for name in changed
        if name in ("remote_poll_seconds", "remote_control_enabled",
                    "device_check_interval_hours", "bot_check_interval_minutes",
                    "rustdesk_check_interval_minutes")
    )
    if needs_restart:
        result["needs_restart"] = needs_restart
        result["note"] = (
            f"Wirksam erst nach einem Neustart des Dienstes: {', '.join(needs_restart)}"
        )

    return result


def _cmd_cleanup(args: dict) -> dict:
    """Alte Logs im Bucket sofort wegräumen, ohne auf den Tageslauf zu warten.

    Der normale Lauf ist auf einmal täglich gedrosselt. Nach einer Regeländerung
    wäre der Rückstand sonst bis zum nächsten Tag stehen geblieben.
    """
    from device_monitor import _get_sb_client
    from log_uploader import RETENTION_DAYS, _cleanup_old_logs

    client = _get_sb_client()
    if client is None:
        raise CommandError("kein Supabase-Client (Key fehlt?)")

    retention = int(args.get("retention_days") or RETENTION_DAYS)
    server_name = get_config().server_name

    # Ein Durchlauf listet höchstens 1000 Objekte. Solange etwas gelöscht wird,
    # kann noch mehr dahinterliegen — also wiederholen, aber begrenzt.
    total, rounds = 0, 0
    while rounds < 10:
        removed = _cleanup_old_logs(client, server_name, retention, force=True)
        total += removed
        rounds += 1
        if removed == 0:
            break

    return {"deleted": total, "rounds": rounds, "retention_days": retention}


def _cmd_customer_stats(args: dict) -> dict:
    from customer_stats import run_customer_stats_upload

    dry_run = bool(args.get("dry_run", False))
    return run_customer_stats_upload(
        trigger="preview" if dry_run else "manual", dry_run=dry_run
    )


def _cmd_bot(args: dict) -> dict:
    import bot_manager

    action = (args.get("action") or "").lower()
    actions = {
        "start": bot_manager.start_bot,
        "stop": bot_manager.stop_bot,
        "restart": bot_manager.restart_bot,
    }
    if action not in actions:
        raise CommandError(f"Unbekannte Aktion '{action}' (erlaubt: start, stop, restart)")

    success = actions[action]()
    return {
        "action": action,
        "success": bool(success),
        "running": bot_manager.is_bot_running(),
        "auto_restart": bot_manager.is_auto_restart_enabled(),
    }


def _cmd_rustdesk(args: dict) -> dict:
    import rustdesk_manager

    action = (args.get("action") or "").lower()
    if action == "start":
        # Wie im Dashboard: Wächter einschalten *und* sofort starten
        rustdesk_manager.set_watch_enabled(True)
        success = rustdesk_manager.start_rustdesk()
    elif action == "stop":
        # Schaltet nur den Wächter ab, beendet kein laufendes RustDesk —
        # sonst würde der Fernzugriff den letzten Notzugang kappen.
        rustdesk_manager.set_watch_enabled(False)
        success = True
    else:
        raise CommandError(f"Unbekannte Aktion '{action}' (erlaubt: start, stop)")

    return {
        "action": action,
        "success": bool(success),
        "running": rustdesk_manager.is_rustdesk_running(),
        "watch": rustdesk_manager.is_watch_enabled(),
    }


def _cmd_update(args: dict) -> dict:
    """Auf eine Version wechseln.

    Achtung: bei Erfolg beendet sich der Prozess zwei Sekunden später selbst
    (launchd startet ihn neu). Der Aufrufer muss das Ergebnis *vor* Ablauf
    dieser Frist zurückgeschrieben haben — darum kümmert sich `remote_agent`.
    """
    from updater import perform_update

    return perform_update(args.get("version") or None)


# ── Warn-Archive des Bots (rein lesend) ───────────────────────────────


def _warning_dirs() -> list:
    from warnings_access import default_bases, find_warning_dirs

    return find_warning_dirs(default_bases(get_config().sqlite_db_path))


def _cmd_warnings(args: dict) -> dict:
    """Die ZIPs in den warnings-Ordnern des Bots, neueste zuerst."""
    from warnings_access import list_warnings

    return list_warnings(_warning_dirs(), args.get("grep") or None)


def _cmd_warning_text(args: dict) -> dict:
    """Was im Warn-ZIP steht: Dateien, Bildschirmtexte aus dem UI-Abbild, Log-Ende."""
    from warnings_access import WarningsError, resolve_warning, warning_text

    try:
        return warning_text(resolve_warning(_warning_dirs(), args.get("name")))
    except WarningsError as e:
        raise CommandError(str(e)) from e


def _cmd_warning_part(args: dict) -> dict:
    """Ein Stück einer Datei aus dem Warn-ZIP (base64), z. B. der Screenshot."""
    from warnings_access import WarningsError, resolve_warning, warning_part

    try:
        offset = int(args.get("offset") or 0)
    except (TypeError, ValueError) as e:
        raise CommandError("offset muss eine Zahl sein") from e
    try:
        return warning_part(resolve_warning(_warning_dirs(), args.get("name")), args.get("member") or "", offset)
    except WarningsError as e:
        raise CommandError(str(e)) from e


# ── Registrierung ─────────────────────────────────────────────────────

# name -> (handler, ist_eingreifend)
HANDLERS: dict[str, tuple] = {
    "status": (_cmd_status, False),
    "config": (_cmd_config, False),
    "logs": (_cmd_logs, False),
    "files": (_cmd_files, False),
    "diag-upload": (_cmd_diag_upload, False),
    "diag-db": (_cmd_diag_db, False),
    "versions": (_cmd_versions, False),
    "legacy-upload": (_cmd_legacy_upload, False),
    "warnings": (_cmd_warnings, False),
    "warning-text": (_cmd_warning_text, False),
    "warning-part": (_cmd_warning_part, False),
    "sync": (_cmd_sync, True),
    "cleanup": (_cmd_cleanup, True),
    "set": (_cmd_set, True),
    "customer-stats": (_cmd_customer_stats, True),
    "bot": (_cmd_bot, True),
    "rustdesk": (_cmd_rustdesk, True),
    "update": (_cmd_update, True),
}

# Befehle, nach denen sich der Prozess selbst beendet — das Ergebnis muss
# vorher in der Queue stehen, sonst bleibt die Zeile für immer auf "running".
SELF_RESTARTING = {"update"}


def describe() -> list[dict]:
    """Die Befehlsliste, wie sie das Dashboard anzeigen kann."""
    return [
        {"command": name, "requires_actions": needs_actions}
        for name, (_, needs_actions) in sorted(HANDLERS.items())
    ]


def execute(command: str, args: dict | None = None) -> dict:
    """Einen Befehl ausführen und sein Ergebnis liefern.

    Wirft `CommandError` bei unbekanntem Namen, falschen Argumenten oder wenn
    eingreifende Befehle abgeschaltet sind. Alles andere schlägt als die
    Ausnahme durch, die der Handler geworfen hat — der Aufrufer schreibt sie
    als `error` in die Queue.
    """
    args = args or {}
    entry = HANDLERS.get(command)
    if entry is None:
        raise CommandError(
            f"Unbekannter Befehl '{command}' (bekannt: {', '.join(sorted(HANDLERS))})"
        )

    handler, needs_actions = entry
    if needs_actions and not get_config().remote_allow_actions:
        raise CommandError(
            f"'{command}' greift ein und ist auf diesem Mac gesperrt "
            f"(remote_allow_actions = false)"
        )

    logger.info(f"Fernzugriff führt aus: {command} {args or ''}".strip())
    return handler(args)
