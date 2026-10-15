"""Lokale Schnittstelle der Bot-App ("Instagram Bot Local Bridge", 127.0.0.1:8765).

Die Bot-App beantwortet /api/* und /ui/* nur vom Mac selbst — hier läuft das MacTool.
/ui/* geht ohne Schlüssel, /api/* verlangt das Admin-Token der Bot-App (pro Mac verschieden,
in der Bot-App unter API Gateway -> Admin token). Es wird nur im MacTool-Dashboard dieses
Macs eingetragen (config `bot_admin_token`, geheim — nie über den Fernzugriff).

Ziel: bei "Mac updaten" nur die betroffenen Accounts anhalten statt die ganze Bot-App
(POST /api/profiles/{id}/stop|start). Dieses Modul kann bisher NUR LESEN.
"""

import logging

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
