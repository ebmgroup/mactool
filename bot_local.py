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


def slot_starts_within(slot: str | None, now_minutes: int, minutes: int) -> bool:
    """Beginnt ein Zeitfenster von `slot` in den nächsten `minutes` Minuten (jetzt ausgenommen)?

    Anders als in_timeslot zählt "run manually" hier NICHT: so ein Account startet nicht
    von selbst. Über Mitternacht wird mitgerechnet (23:55 + 10 min trifft 00:00).
    """
    for a, _ in re.findall(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", (slot or "").lower()):
        start = int(a[:-3]) * 60 + int(a[-2:])
        if 0 < (start - now_minutes) % 1440 <= minutes:
            return True
    return False


# ── Config eines Accounts ändern (bot-config) ─────────────────────────
#
# Weg belegt im Quellcode der Bot-App (tasks/automatik-2026-10-27/A6_botapi.md):
#   PATCH /api/profiles/{id}/config     {"parameter": p, "value": v}  -> config.save
#   PATCH /api/profiles/{id}/targeting  {"sources": {"blogger-followers": {value, enabled}}}
# Kein Konfliktschutz auf der Bot-Seite: deshalb Altwert unmittelbar vorher mit `expect`
# vergleichen und hinterher nachlesen. Genau EIN Account, genau EIN Parameter pro Aufruf.

RANGE_RE = re.compile(r"^(\d{1,3})(?:-(\d{1,3}))?$")
HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")
MAX_TARGETS = 50

# param -> (Endpunkt, Feldpfade unter profile.details.config, Obergrenze)
CONFIG_PARAMS = {
    "total-follows-limit": ("config", (("follow-input", "value"),
                                       ("follow-input", "params", "follow-limit"),
                                       ("follow-input", "params", "total-follows-limit")), 80),
    "total-unfollows-limit": ("config", (("unfollow-input", "value"),
                                         ("unfollow-input", "params", "unfollow"),
                                         ("unfollow-input", "params", "total-unfollows-limit")), 80),
    "stop-unfollow-after": ("config", (("stop-unfollow-after",),), 60),
    "blogger-followers": ("targeting", (("blogger-followers",),), None),
}


def _text(value) -> str:
    """macctl macht aus "20" eine Zahl, aus "" bleibt "" — für den Vergleich immer Text."""
    if value is None:
        return ""
    if isinstance(value, bool):
        raise BotLocalError("Wert darf kein ja/nein sein")
    return str(value).strip()


def _handles(value) -> list[str]:
    """Kommaliste wie die Bot-App sie speichert (_source_names): getrimmt, ohne Doppelte."""
    names, seen = [], set()
    for item in _text(value).replace("\n", ",").split(","):
        name = item.strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def validate_config_value(param: str, value) -> str:
    """Neuen Wert prüfen und in die Form bringen, in der er gesendet wird."""
    if param not in CONFIG_PARAMS:
        raise BotLocalError(f"param muss {', '.join(CONFIG_PARAMS)} sein")
    _, _, maximum = CONFIG_PARAMS[param]
    text = _text(value)
    if param == "blogger-followers":
        names = _handles(text)
        bad = [n for n in names if not HANDLE_RE.match(n)]
        if bad:
            raise BotLocalError(f"Ungültige Usernamen (erlaubt a-z 0-9 . _): {', '.join(bad[:5])}")
        if len(names) > MAX_TARGETS:
            raise BotLocalError(f"Höchstens {MAX_TARGETS} Ziel-Accounts")
        return ",".join(names)
    match = RANGE_RE.match(text)
    if not match:
        raise BotLocalError(f"{param}: Format Zahl oder Bereich wie 20-25, bekam {text!r}")
    low, high = int(match.group(1)), int(match.group(2) or match.group(1))
    if high < low:
        raise BotLocalError(f"{param}: Obergrenze {high} kleiner als Untergrenze {low}")
    if high > maximum:
        raise BotLocalError(f"{param}: höchstens {maximum}, bekam {text}")
    return text


def _dig(config: dict, path: tuple):
    node = config
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _field_values(profile_body: dict, param: str) -> dict:
    config = ((profile_body.get("details") or {}).get("config") or {})
    _, paths, _ = CONFIG_PARAMS[param]
    return {".".join(path): _dig(config, path) for path in paths}


def _norm(param: str, value) -> str:
    return ",".join(_handles(value)) if param == "blogger-followers" else _text(value)


def full_profile(profile_id: str, token: str) -> dict:
    """GET /api/profiles/{id} -> der komplette Datensatz (mit details.config)."""
    status, body = get(f"/api/profiles/{profile_id}", token)
    if status != 200 or not isinstance(body, dict):
        raise BotLocalError(f"Account {profile_id} nicht lesbar (HTTP {status})")
    return body.get("profile", body)


def targeting_state(profile_id: str) -> dict:
    """Ziel-Quelle blogger-followers mit enabled-Schalter — steht in keinem GET.

    Rein lesend: fest verdrahtet NUR kind=profile.targeting.read (die Oberfläche der Bot-App
    liest so beim Öffnen des Targeting-Dialogs). Kein anderer /ui/command-Befehl ist möglich.
    """
    response = requests.post(f"{BASE}/ui/command?lite=1", timeout=TIMEOUT,
                             headers={"Accept": "application/json"},
                             json={"kind": "profile.targeting.read", "profileId": str(profile_id)})
    body = _body(response)
    result = body.get("result", body) if isinstance(body, dict) else {}
    source = ((result or {}).get("sources") or {}).get("main-target")
    if response.status_code != 200 or not isinstance(source, dict):
        raise BotLocalError(f"Targeting von {profile_id} nicht lesbar (HTTP {response.status_code})")
    return {"value": str(source.get("value") or ""), "enabled": bool(source.get("enabled"))}


def rendered_targets_active(username: str) -> bool | None:
    """Was der Bot wirklich bekommt: steht `blogger-followers:` in config.yml aktiv (True),
    auskommentiert (False) oder gar nicht (None)?

    Nötig, weil profile.targeting.read einen FEHLENDEN target-source-state-Eintrag als false
    meldet, der Renderer ihn aber als aktiv behandelt (SetupWriter, 1.4.0.60). Wer den gelesenen
    Wert blind zurückschickt, legt bei solchen Accounts die Zielliste still.
    """
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username or ""):
        return None
    from pathlib import Path

    path = Path(get_config().sqlite_db_path).expanduser().parent / "accounts" / username / "config.yml"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        match = re.match(r"^\s*(#?)\s*blogger-followers\s*:", line)
        if match:
            return match.group(1) != "#"
    return None


def _patch(path: str, token: str, payload: dict) -> tuple[int, object]:
    response = requests.patch(BASE + path, json=payload, timeout=60,
                              headers={"Accept": "application/json", "X-API-Key": token})
    return response.status_code, _body(response)


def _short(body, limit: int = 4000):
    import json

    text = json.dumps(body, ensure_ascii=False, default=str)
    return body if len(text) <= limit else text[:limit] + " …(gekürzt)"


def bot_config(profile_id, param: str, value, expect, dry: bool = True,
               now_minutes: int | None = None, enabled: bool | None = None) -> dict:
    """Genau EINEN Config-Wert eines Accounts ändern. Standard: nur Plan (dry)."""
    profile_id = _text(profile_id)
    if profile_id.lower() in ("all", "new", "*") or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile_id):
        raise BotLocalError("profile muss genau eine Account-ID sein (nie all/new)")
    if expect is None:
        raise BotLocalError("expect=<aktueller Wert> fehlt (Schutz gegen veraltete Werte)")
    new_value = validate_config_value(param, value)
    endpoint, _, _ = CONFIG_PARAMS[param]
    token = get_config().bot_admin_token or ""
    if not token:
        raise BotLocalError("bot_admin_token ist nicht gesetzt")

    before = full_profile(profile_id, token)
    fields = _field_values(before, param)
    head = {"profile": profile_id, "username": before.get("username"), "param": param,
            "runState": before.get("runState"), "timeSlot": before.get("timeSlot"),
            "source": before.get("source")}

    def refuse(reason: str) -> dict:
        logger.info(f"Bot-App: bot-config {profile_id} {param} abgelehnt: {reason}")
        return {**head, "status": "refused", "reason": reason, "before": fields}

    if before.get("platform") not in (None, "instagram"):
        return refuse(f"Plattform {before.get('platform')} — nur instagram")
    if "running" in (before.get("runState"), before.get("sessionStatus")):
        return refuse("Account läuft gerade (runState running)")
    if now_minutes is None:
        from datetime import datetime

        now = datetime.now()   # Mac-Ortszeit, wie die Bot-App plant
        now_minutes = now.hour * 60 + now.minute
    if slot_starts_within(before.get("timeSlot"), now_minutes, 5):
        return refuse("Zeitfenster beginnt in den nächsten 5 Minuten")
    distinct = {_norm(param, v) for v in fields.values()}
    if len(distinct) != 1:
        return refuse(f"Felder nicht einheitlich: {fields}")
    current = distinct.pop()
    if current != _norm(param, expect):
        return refuse(f"aktueller Wert {current!r} ≠ expect {_norm(param, expect)!r}")

    targeting_before = None
    if endpoint == "targeting":
        targeting_before = targeting_state(profile_id)
        if _norm(param, targeting_before["value"]) != current:
            return refuse("Targeting und Config widersprechen sich: "
                          f"{targeting_before['value']!r} / {current!r}")
        # Der PATCH setzt enabled immer (fehlt er: false). Ohne Vorgabe bleibt, was der Bot heute
        # bekommt: aktiv, wenn gelesen true ODER die Zeile in config.yml aktiv steht.
        rendered = rendered_targets_active(str(before.get("username") or ""))
        if enabled is None:
            enabled = bool(targeting_before["enabled"] or rendered)
        payload = {"sources": {"blogger-followers": {"value": new_value, "enabled": bool(enabled)}}}
        head.update({"enabled": bool(enabled), "enabled_read": targeting_before["enabled"],
                     "config_yml_active": rendered})
    else:
        payload = {"parameter": param, "value": new_value}
    plan = {"method": "PATCH", "path": f"/api/profiles/{profile_id}/{endpoint}", "body": payload}

    if current == new_value and (endpoint != "targeting" or bool(enabled) == targeting_before["enabled"]):
        return {**head, "status": "unchanged", "plan": plan, "before": fields, "after": fields}
    if dry:
        return {**head, "status": "dry", "plan": plan, "before": fields}

    status, response = _patch(plan["path"], token, payload)
    logger.info(f"Bot-App: bot-config {profile_id} ({head['username']}) {param} "
                f"{current!r} -> {new_value!r}: HTTP {status}")
    after = full_profile(profile_id, token)
    after_fields = _field_values(after, param)
    ok = status == 200 and all(_norm(param, v) == new_value for v in after_fields.values())
    result = {**head, "status": "ok" if ok else "reverted_or_failed", "http": status,
              "plan": plan, "before": fields, "after": after_fields, "response": _short(response)}
    if endpoint == "targeting":
        if result["status"] != "ok" and len(_handles(new_value)) < len(_handles(current)):
            # Belegt mac17, 27.10.: Liste von 2 auf 0 -> HTTP 200, Liste unverändert
            result["hint"] = ("Bot-App hält einen Mindestbestand an Ziel-Accounts "
                              "(source_property doNotAllowDeleteLessThan) — kürzer geht über "
                              "/targeting und die Oberfläche nicht")
        targeting_after = targeting_state(profile_id)
        result["enabled_after"] = targeting_after["enabled"]
        if targeting_after["enabled"] != bool(enabled):
            result["status"] = "reverted_or_failed"
    return result


# ── bot-check: Zustand der Bot-App für die Automatik (rein lesend) ────
#
# Meldet NUR Ja/Nein und Zahlen (plus die Versionsnummer der Bot-App) — nie Schlüssel, URLs,
# Pfade oder Fehlermeldungen. Quellen belegt im Code der Bot-App 1.4.0.60:
#   - Supabase-Abruf:  super.db, Tabelle `supabase`, Spalte `enableFetch` (SuperDataStoreApp,
#     als BOOL über safe_bool gelesen). Dieselbe Zeile enthält URL + Key — gelesen wird NUR die
#     eine Spalte.
#   - followunfollow.txt: GramBotStorage/accounts/<user>/followunfollow.txt; wirkt nur mit genau
#     "True"/"False" und nur bei enableFetch (SetupWriter.supabase_work_around: setzt dann die
#     Follow-/Unfollow-/Like-Limits auf die globalen Supabase-Werte zurück).
#   - Mindestbestand: GramBotStorage/source_property.json -> defualt_settings ->
#     doNotAllowDeleteLessThan {enabled, value} (_shared._source_keep_guard).

VERSION_RE = re.compile(r"^\d+(?:\.\d+){1,4}$")
BOT_APP_CONSTANTS = "/Applications/BotApp.app/Contents/Resources/resources/botapp-runtime/Constants.py"


def _storage_dir():
    from pathlib import Path

    return Path(get_config().sqlite_db_path).expanduser().parent


def _coerce_bool(value) -> bool:
    """Wie die Bot-App (_shared._coerce_bool / safe_parse.safe_bool)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().strip("'\"").lower() in {"1", "true", "yes", "on"}


def bot_app_version() -> str | None:
    """APP_VERSION der installierten Bot-App (Constants.py im Bundle).

    /api/health meldet eine veraltete Zahl (A6: 1.4.0.59 bei laufender 1.4.0.60), Info.plist
    ein verkürztes "1.4.60". Constants.py ist die Zahl, die die App selbst anzeigt.
    """
    from pathlib import Path

    path = Path(BOT_APP_CONSTANTS)
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            head = fh.read(4000)
    except OSError:
        return None
    match = re.search(r"^APP_VERSION\s*=\s*['\"]([^'\"]+)['\"]", head, re.M)
    if match and VERSION_RE.match(match.group(1)):
        return match.group(1)
    return None


def supabase_fetch_enabled(db_path=None) -> bool | None:
    """enableFetch aus super.db (nur diese Spalte, nur lesend). None = nicht lesbar."""
    import sqlite3
    from pathlib import Path

    path = Path(db_path or get_config().sqlite_db_path).expanduser()
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute('SELECT "enableFetch" FROM supabase WHERE id = ?', ("1",)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return _coerce_bool(row[0])


def followunfollow_counts(storage=None) -> dict | None:
    """Account-Ordner unter GramBotStorage/accounts: wie viele haben followunfollow.txt?

    Nur dieser eine Ordner, eine Ebene tief — kein Durchsuchen des Home-Verzeichnisses.
    `true`/`false` = Dateien mit genau diesem Inhalt (nur die greifen im SetupWriter).
    """
    accounts = (storage or _storage_dir()) / "accounts"
    if not accounts.is_dir():
        return None
    folders = with_file = true = false = 0
    for entry in accounts.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        folders += 1
        marker = entry / "followunfollow.txt"
        if not marker.is_file():
            continue
        with_file += 1
        try:
            text = marker.read_text(encoding="utf-8", errors="replace")[:20]
        except OSError:
            continue
        true += text == "True"
        false += text == "False"
    return {"ordner": folders, "mit_datei": with_file, "true": true, "false": false}


def source_keep_guard(storage=None) -> dict:
    """doNotAllowDeleteLessThan aus der globalen source_property.json: an? Grenzwert?"""
    import json

    path = (storage or _storage_dir()) / "source_property.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"an": None, "wert": None}
    defaults = data.get("defualt_settings") if isinstance(data, dict) else None
    guard = defaults.get("doNotAllowDeleteLessThan") if isinstance(defaults, dict) else None
    if not isinstance(guard, dict):
        return {"an": False, "wert": None}
    try:
        value = max(0, int(float(str(guard.get("value")).replace("'", "").strip())))
    except (TypeError, ValueError):
        value = 0     # wie _guard_int(…, 0)
    return {"an": _coerce_bool(guard.get("enabled")), "wert": value}


def bot_check() -> dict:
    """Rein lesender Überblick für die Dashboard-Automatik. Nur Ja/Nein und Zahlen."""
    result = {"bot_app_erreichbar": None, "bot_app_version": None,
              "admin_token_gesetzt": bool(get_config().bot_admin_token or ""),
              "admin_token_gueltig": None, "supabase_abruf_an": None,
              "followunfollow_txt": None, "ta_min_loeschen": None, "ta_min_loeschen_an": None,
              "fehler": []}

    def step(name, fn):
        try:
            return fn()
        except Exception:      # nie die Meldung weitergeben — sie kann Pfade/Inhalte enthalten
            result["fehler"].append(name)
            return None

    connection = step("bot_app", test_connection) or {}
    if connection:
        result["bot_app_erreichbar"] = bool(connection.get("ok") or connection.get("reachable"))
        if result["admin_token_gesetzt"] and result["bot_app_erreichbar"]:
            result["admin_token_gueltig"] = bool(connection.get("ok"))
        if isinstance(connection.get("devices"), int):
            result["geraete"] = connection["devices"]
    result["bot_app_version"] = step("bot_app_version", bot_app_version)
    result["supabase_abruf_an"] = step("supabase_abruf", supabase_fetch_enabled)
    result["followunfollow_txt"] = step("followunfollow_txt", followunfollow_counts)
    guard = step("ta_min_loeschen", source_keep_guard) or {}
    result["ta_min_loeschen_an"] = guard.get("an")
    result["ta_min_loeschen"] = guard.get("wert") if guard.get("an") else None
    counts = result["followunfollow_txt"] or {}
    if result["supabase_abruf_an"] is not None:
        # Der SetupWriter dreht Limits dieser Accounts beim nächsten Rendern auf Supabase-Werte
        result["supabase_ueberschreibt_limits"] = bool(
            result["supabase_abruf_an"] and (counts.get("true") or counts.get("false")))
    return result
