"""Während ein Phone aktualisiert wird: dessen Bot-Accounts anhalten, danach wieder starten.

Statt die ganze Bot-App zu stoppen (Andreas, 15.10.): nur die Accounts des Phones, das gerade
dran ist. Belegt auf mac08 (Account 85): /api/profiles/{id}/stop beendet die Session sofort,
der Account bleibt danach gestoppt (startet nicht von selbst neu), /start startet ihn wieder.

Ablauf je Phone:
  1. laufende Accounts stoppen
  2. alle WATCH_SECONDS prüfen: startet einer (Zeitplan), sofort wieder stoppen
  3. nach dem Update alle von uns gestoppten wieder starten — nur wenn ihr Zeitfenster noch
     läuft (oder "run manually"); sonst bleiben sie aus, die nächste Session kommt planmäßig

Welche Accounts wir angehalten haben, steht in STATE_FILE — nach einem Neustart des MacTools
holt resume_pending() das Wiederstarten nach.
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import bot_local

logger = logging.getLogger(__name__)

WATCH_SECONDS = 10
STATE_FILE = Path(__file__).parent / "ig_stopped_accounts.json"
_file_lock = threading.Lock()


def _now_minutes() -> int:
    now = datetime.now()   # Mac-Ortszeit — mit der plant auch die Bot-App
    return now.hour * 60 + now.minute


def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save(data: dict) -> None:
    if data:
        STATE_FILE.write_text(json.dumps(data, indent=1))
    else:
        STATE_FILE.unlink(missing_ok=True)


def _remember(serial: str, account: dict) -> None:
    with _file_lock:
        data = _load()
        data.setdefault(serial, {})[account["id"]] = {
            "username": account.get("username"), "timeSlot": account.get("timeSlot")}
        _save(data)


def _forget(serial: str) -> None:
    with _file_lock:
        data = _load()
        data.pop(serial, None)
        _save(data)


def available() -> bool:
    """Ist die lokale Bot-Schnittstelle mit gültigem Schlüssel erreichbar?"""
    try:
        return bool(bot_local.test_connection().get("ok"))
    except Exception:
        return False


class AccountGuard:
    def __init__(self, serial: str, progress=None):
        self.serial = serial
        self.progress = progress or (lambda text: None)
        self.stopped: dict[str, dict] = {}
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self.errors: list[str] = []

    def _stop_running(self) -> int:
        count = 0
        for account in bot_local.device_profiles(self.serial):
            if account.get("runState") != "running":
                continue
            result = bot_local.profile_action(account["id"], "stop")
            if result.get("status") == 200:
                self.stopped[account["id"]] = account
                _remember(self.serial, account)
                count += 1
                logger.info(f"[{self.serial}] Account {account['username']} angehalten (Update)")
            else:
                self.errors.append(f"Stopp {account['username']}: HTTP {result.get('status')}")
        return count

    def _watch(self):
        while not self._halt.wait(WATCH_SECONDS):
            try:
                if self._stop_running():
                    self.progress("ein Account wollte starten — angehalten")
            except Exception as e:
                logger.warning(f"[{self.serial}] Wächter: {e}")

    def __enter__(self):
        if self._stop_running():
            self.progress(f"{len(self.stopped)} laufende(n) Account(s) angehalten")
            time.sleep(3)   # Prozess beenden lassen, bevor wir das Phone bedienen
        self._thread = threading.Thread(target=self._watch, name=f"guard-{self.serial}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._halt.set()
        if self._thread:
            self._thread.join(timeout=WATCH_SECONDS + 5)
        self.restarted, self.left_off = restart(self.serial, self.stopped)
        _forget(self.serial)
        return False

    def summary(self) -> dict:
        return {"angehalten": [a.get("username") for a in self.stopped.values()],
                "wieder_gestartet": getattr(self, "restarted", []),
                "bleibt_aus (Zeitfenster vorbei)": getattr(self, "left_off", []),
                **({"fehler": self.errors} if self.errors else {})}


def restart(serial: str, stopped: dict[str, dict]) -> tuple[list, list]:
    restarted, left_off = [], []
    now = _now_minutes()
    for account_id, account in stopped.items():
        name = account.get("username") or account_id
        if not bot_local.in_timeslot(account.get("timeSlot"), now):
            left_off.append(name)
            continue
        try:
            result = bot_local.profile_action(account_id, "start")
            (restarted if result.get("status") == 200 else left_off).append(name)
        except Exception as e:
            logger.warning(f"[{serial}] Account {name} nicht wieder gestartet: {e}")
            left_off.append(name)
    return restarted, left_off


def resume_pending() -> dict:
    """Nach einem Neustart des MacTools: noch angehaltene Accounts wieder starten."""
    data = _load()
    done = {}
    for serial, stopped in data.items():
        done[serial] = restart(serial, stopped)
    _save({})
    return done
