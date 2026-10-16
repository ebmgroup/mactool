"""Lokale Schnittstelle der Bot-App ("Instagram Bot Local Bridge", 127.0.0.1:8765).

Die Bot-App beantwortet /api/* und /ui/* nur vom Mac selbst — hier läuft das MacTool.
/ui/* geht ohne Schlüssel, /api/* verlangt das Admin-Token der Bot-App (pro Mac verschieden,
in der Bot-App unter API Gateway -> Admin token). Es wird nur im MacTool-Dashboard dieses
Macs eingetragen (config `bot_admin_token`, geheim — nie über den Fernzugriff).

Ziel: bei "Mac updaten" nur die betroffenen Accounts anhalten statt die ganze Bot-App
(POST /api/profiles/{id}/stop|start). Lesen + genau diese drei Aktionen je EIN Account.
"""

import logging
import re

import requests

from config import get_config

logger = logging.getLogger(__name__)

BASE = "http://127.0.0.1:8765"
# Die Fehlermeldung ohne Schlüssel nennt X-API-Key, die Bot-App-Oberfläche X-Admin-Key —
# welcher gilt, findet test_connection heraus.
TOKEN_HEADERS = ("X-API-Key", "X-Admin-Key")
TIMEOUT = 20


class BotLocalError(RuntimeError):
    pass


def _body(response):
    try:
        return response.json()
    except ValueError:
        return response.text[:2000]


def get(path: str, token: str | None = None, header: str | None = None) -> tuple[int, object]:
    """GET auf die lokale Schnittstelle. Nur /api/… und /ui/…."""
    if not path.startswith(("/api/", "/ui/")) or ".." in path or "://" in path:
        raise BotLocalError("path muss mit /api/ oder /ui/ beginnen")
    headers = {"Accept": "application/json"}
    if token:
        headers[header or TOKEN_HEADERS[0]] = token
    response = requests.get(BASE + path, headers=headers, timeout=TIMEOUT)
    return response.status_code, _body(response)


def test_connection(token: str | None = None) -> dict:
    """Lesend prüfen, ob die Bot-App lokal erreichbar ist und das Admin-Token passt."""
    token = token if token is not None else (get_config().bot_admin_token or "")
    try:
        status, _ = get("/ui/runtime/instagram")
    except requests.RequestException as e:
        return {"ok": False, "message": f"Bot-App lokal nicht erreichbar ({type(e).__name__}) — läuft sie?"}
    if status != 200:
        return {"ok": False, "message": f"Bot-App antwortet unerwartet (HTTP {status})"}
    if not token:
        return {"ok": False, "reachable": True, "message": "Bot-App erreichbar, aber kein Admin-Token eingetragen"}
    tried = {}
    for header in TOKEN_HEADERS:
        status, body = get("/api/devices", token, header)
        tried[header] = status
        if status == 200:
            devices = body.get("devices") if isinstance(body, dict) else body
            count = len(devices) if isinstance(devices, list) else None
            return {"ok": True, "header": header, "devices": count,
                    "message": f"Verbindung ok ({count} Geräte, Kopfzeile {header})"}
    return {"ok": False, "reachable": True, "tried": tried,
            "message": "Admin-Token wird nicht angenommen (" +
                       ", ".join(f"{h}: HTTP {s}" for h, s in tried.items()) + ")"}


PROFILE_ACTIONS = ("stop", "start", "restart")


def profile(profile_id: str, token: str | None = None) -> dict:
    """Ein Account: runState, currentAction, Gerät, Kopie, Zeitfenster."""
    token = token if token is not None else (get_config().bot_admin_token or "")
    status, body = get(f"/api/profiles/{profile_id}", token)
    if status != 200 or not isinstance(body, dict):
        raise BotLocalError(f"Account {profile_id} nicht lesbar (HTTP {status})")
    p = body.get("profile", body)
    return {k: p.get(k) for k in ("id", "username", "deviceId", "apkCloner", "runState",
                                  "currentAction", "sessionStatus", "sessionTime", "timeSlot", "scheduled")}


def profile_action(profile_id: str, action: str) -> dict:
    """Genau EINEN Account stoppen / starten / neu starten. Vorher und nachher lesen."""
    if action not in PROFILE_ACTIONS:
        raise BotLocalError(f"action muss {', '.join(PROFILE_ACTIONS)} sein")
    # IDs sind Zahlen ODER Hex wie "89e1f75530b55bd9" (mac06/mac14, 16.10.)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(profile_id)):
        raise BotLocalError("profile muss eine Account-ID sein (Buchstaben/Ziffern)")
    token = get_config().bot_admin_token or ""
    if not token:
        raise BotLocalError("bot_admin_token ist nicht gesetzt")
    before = profile(profile_id, token)
    response = requests.post(f"{BASE}/api/profiles/{profile_id}/{action}",
                             headers={"Accept": "application/json", "X-API-Key": token}, timeout=60)
    logger.info(f"Bot-App: Account {profile_id} ({before.get('username')}) {action} -> HTTP {response.status_code}")
    return {"action": action, "status": response.status_code, "response": _body(response),
            "before": before, "after": profile(profile_id, token)}


def device_profiles(device_id: str, token: str | None = None) -> list[dict]:
    """Alle Accounts eines Phones (Bot-Geräte-ID = adb-Seriennummer) mit runState und Zeitfenster."""
    token = token if token is not None else (get_config().bot_admin_token or "")
    status, body = get("/api/profiles", token)
    if status != 200 or not isinstance(body, dict):
        raise BotLocalError(f"Accounts nicht lesbar (HTTP {status})")
    return [
        {"id": str(p.get("id")), "username": p.get("username"), "runState": p.get("runState"),
         "timeSlot": p.get("timeSlot"), "apkCloner": p.get("apkCloner")}
        for p in body.get("profiles", []) if p.get("deviceId") == device_id
    ]


def in_timeslot(slot: str | None, now_minutes: int) -> bool:
    """Liegt `now_minutes` (Minuten seit Mitternacht, Mac-Ortszeit) in "02:00-03:59 + 14:00-15:59"?

    "run manually" zählt als ja: so ein Account läuft nur, wenn ihn jemand gestartet hat —
    wenn wir ihn angehalten haben, soll er weiterlaufen.
    """
    import re

    text = (slot or "").strip().lower()
    if not text or "manual" in text:
        return True
    for a, b in re.findall(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", text):
        start = int(a[:-3]) * 60 + int(a[-2:])
        end = int(b[:-3]) * 60 + int(b[-2:])
        if (start <= now_minutes <= end) if start <= end else (now_minutes >= start or now_minutes <= end):
            return True
    return False
