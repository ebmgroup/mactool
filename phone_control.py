"""Phone-Zustand lesen, Samsung-Updates ab-/anschalten, Phone neu starten — je GENAU EIN Phone.

Für die Dashboard-Automatik von SK TEST 3 (tasks/automatik-2026-10-27/A1_phone.md):
- `phone-info`       lesend: Uptime, Speicher, Update-Pakete, adb, Modell, Instagram vorne?
- `samsung-updates`  status lesend; off/on greifen ein (feste Paketliste, fester Setting-Schlüssel)
- `phone-reboot`     greift ein; verweigert, solange ein Account des Phones läuft oder gleich startet

Eingreifendes ist immer `dry=true`, bis ausdrücklich `dry=false` kommt. Kein freier Paketname,
kein freier Shell-Befehl als Argument.
"""

import logging
import re
import subprocess
import time
from datetime import datetime

from apk_rollout import RolloutError, _adb, _shell

logger = logging.getLogger(__name__)

SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
UPDATE_PACKAGES = ("com.wssyncmldm", "com.sec.android.soagent")
OTA_SETTING = "ota_disable_automatic_update"
BOOT_TIMEOUT = 180
BOOT_POLL = 5
SLOT_LEAD_MINUTES = 15

# Für Tests austauschbar
_sleep = time.sleep
_monotonic = time.monotonic


def _serial(serial) -> str:
    serial = str(serial or "").strip()
    if not SERIAL_RE.match(serial):
        raise RolloutError("Genau ein Handy angeben: serial=<seriennummer>")
    return serial


def adb_state(adb: str, serial: str) -> str:
    """'device' = online; sonst der Zustand bzw. die Fehlermeldung von adb."""
    try:
        result = subprocess.run([adb, "-s", serial, "get-state"], capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return "timeout"
    return (result.stdout.strip() or result.stderr.strip() or f"exit {result.returncode}")[:200]


def _uptime_seconds(adb: str, serial: str) -> int | None:
    try:
        return int(float(_shell(adb, serial, "cat /proc/uptime").split()[0]))
    except (ValueError, IndexError):
        return None


def _packages(adb: str, serial: str) -> dict:
    """Je Update-Paket: installiert? deaktiviert? (exakte Zeile, pm filtert nur Teilstrings)."""
    result = {}
    for package in UPDATE_PACKAGES:
        line = f"package:{package}"
        installed = line in _shell(adb, serial, f"pm list packages {package}").splitlines()
        disabled = line in _shell(adb, serial, f"pm list packages -d {package}").splitlines()
        result[package] = {"installed": installed, "enabled": installed and not disabled}
    return result


def _ota_setting(adb: str, serial: str) -> str:
    return _shell(adb, serial, f"settings get global {OTA_SETTING}")


def _foreground(adb: str, serial: str) -> str:
    out = _shell(adb, serial, "dumpsys activity activities | grep -m1 -E 'topResumedActivity|mResumedActivity'")
    # grep -m1 schließt die Leitung -> dumpsys meldet "Failed to write … Broken pipe"
    return next((l.strip() for l in out.splitlines() if "Resumed" in l), out.splitlines()[0] if out else "")


def _df_data(adb: str, serial: str) -> str:
    """Die Zeile von /data. Samsung listet bei `df /data` weitere Mounts (obb …) dahinter."""
    lines = [l for l in _shell(adb, serial, "df -h /data").splitlines()[1:] if l.strip()]
    return next((l for l in lines if l.split()[-1] == "/data"), lines[0] if lines else "")


def _manufacturer(adb: str, serial: str) -> str:
    return _shell(adb, serial, "getprop ro.product.manufacturer")


def phone_info(serial) -> dict:
    """Nur lesend. Ist das Phone nicht online, kommt nur der adb-Zustand zurück."""
    serial = _serial(serial)
    adb = _adb()
    state = adb_state(adb, serial)
    info = {"serial": serial, "adb": state, "online": state == "device"}
    if state != "device":
        return info
    focus = _foreground(adb, serial)
    info.update({
        "manufacturer": _manufacturer(adb, serial),
        "model": _shell(adb, serial, "getprop ro.product.model"),
        "android": _shell(adb, serial, "getprop ro.build.version.release"),
        "uptime_seconds": _uptime_seconds(adb, serial),
        "df_data": _df_data(adb, serial),
        "update_packages": _packages(adb, serial),
        OTA_SETTING: _ota_setting(adb, serial),
        "foreground": focus,
        "instagram_vorne": "com.instagram." in focus,
    })
    return info


# ── Samsung-Updates ───────────────────────────────────────────────────


def _update_status(adb: str, serial: str) -> dict:
    return {"manufacturer": _manufacturer(adb, serial), "packages": _packages(adb, serial),
            OTA_SETTING: _ota_setting(adb, serial)}


def _update_plan(action: str) -> list[str]:
    if action == "off":
        return [f"pm disable-user --user 0 {p}" for p in UPDATE_PACKAGES] + \
               [f"settings put global {OTA_SETTING} 1"]
    return [f"pm enable --user 0 {p}" for p in UPDATE_PACKAGES] + [f"settings put global {OTA_SETTING} 0"]


def _update_target_reached(status: dict, action: str) -> bool:
    want_enabled = action == "on"
    return (all(p["enabled"] == want_enabled for p in status["packages"].values())
            and status[OTA_SETTING] == ("0" if want_enabled else "1"))


def samsung_updates(serial, action: str, dry: bool = True) -> dict:
    serial = _serial(serial)
    action = str(action or "").lower()
    if action not in ("status", "off", "on"):
        raise RolloutError("action muss status, off oder on sein")
    adb = _adb()
    state = adb_state(adb, serial)
    if state != "device":
        raise RolloutError(f"Phone {serial} nicht online (adb: {state})")
    before = _update_status(adb, serial)
    head = {"serial": serial, "action": action, "before": before}
    if action == "status":
        return {**head, "status": "status"}
    if "samsung" not in before["manufacturer"].lower():
        return {**head, "status": "refused", "reason": f"kein Samsung-Gerät ({before['manufacturer']!r})"}
    missing = [p for p, s in before["packages"].items() if not s["installed"]]
    if missing:
        return {**head, "status": "refused", "reason": f"Paket(e) nicht vorhanden: {', '.join(missing)}"}
    plan = _update_plan(action)
    if _update_target_reached(before, action):
        return {**head, "status": "unchanged", "plan": plan, "after": before}
    if dry:
        return {**head, "status": "dry", "plan": plan}
    output = {command: _shell(adb, serial, command) for command in plan}
    after = _update_status(adb, serial)
    ok = _update_target_reached(after, action)
    logger.info(f"[{serial}] samsung-updates {action}: {'ok' if ok else 'NICHT erreicht'}")
    return {**head, "status": "ok" if ok else "failed", "plan": plan, "output": output, "after": after}


# ── Neustart ──────────────────────────────────────────────────────────


def _now_minutes() -> int:
    now = datetime.now()   # Mac-Ortszeit — mit der plant die Bot-App
    return now.hour * 60 + now.minute


def reboot_blockers(serial: str, now_minutes: int | None = None) -> tuple[list[str], list[dict]]:
    """Gründe gegen einen Neustart jetzt. Ohne lesbare Bot-App: verweigern statt raten."""
    import bot_local

    now = _now_minutes() if now_minutes is None else now_minutes
    try:
        accounts = bot_local.device_profiles(serial)
    except Exception as e:
        return [f"Accounts des Phones nicht lesbar ({type(e).__name__}: {e})"], []
    reasons = []
    for account in accounts:
        name = account.get("username") or account.get("id")
        if account.get("runState") == "running":
            reasons.append(f"{name} läuft gerade")
        elif bot_local.slot_starts_within(account.get("timeSlot"), now, SLOT_LEAD_MINUTES):
            reasons.append(f"{name}: Zeitfenster beginnt in ≤ {SLOT_LEAD_MINUTES} min ({account.get('timeSlot')})")
    return reasons, accounts


def _wait_for_boot(adb: str, serial: str) -> tuple[bool, int]:
    started = _monotonic()
    while _monotonic() - started < BOOT_TIMEOUT:
        _sleep(BOOT_POLL)
        if adb_state(adb, serial) == "device" and _shell(adb, serial, "getprop sys.boot_completed",
                                                         timeout=15) == "1":
            return True, int(_monotonic() - started)
    return False, int(_monotonic() - started)


def phone_reboot(serial, dry: bool = True, now_minutes: int | None = None) -> dict:
    from device_monitor import restart_adb_device
    from ig_worker import _phone_lock

    serial = _serial(serial)
    adb = _adb()
    state = adb_state(adb, serial)
    if state != "device":
        raise RolloutError(f"Phone {serial} nicht online (adb: {state})")
    reasons, accounts = reboot_blockers(serial, now_minutes)
    lock = _phone_lock(serial)
    if lock.locked():
        reasons.append("auf dem Phone läuft gerade ein Instagram-Update")
    before = {"uptime_seconds": _uptime_seconds(adb, serial), "foreground": _foreground(adb, serial)}
    head = {"serial": serial, "accounts": [{k: a.get(k) for k in ("username", "runState", "timeSlot")}
                                           for a in accounts], "before": before,
            "plan": [f"adb -s {serial} reboot", f"warten auf sys.boot_completed (max {BOOT_TIMEOUT} s)"]}
    if reasons:
        return {**head, "status": "refused", "reasons": reasons}
    if dry:
        return {**head, "status": "dry", "note": "würde jetzt neu starten"}
    if not lock.acquire(blocking=False):
        return {**head, "status": "refused", "reasons": ["Instagram-Update hat das Phone gerade belegt"]}
    try:
        if not restart_adb_device(serial):
            return {**head, "status": "failed", "reason": "adb reboot nicht angenommen"}
        logger.info(f"[{serial}] phone-reboot gesendet")
        booted, seconds = _wait_for_boot(adb, serial)
    finally:
        lock.release()
    after = {"adb": adb_state(adb, serial), "seconds": seconds}
    if booted:
        after["uptime_seconds"] = _uptime_seconds(adb, serial)
    return {**head, "status": "ok" if booted and after["adb"] == "device" else "timeout", "after": after}
