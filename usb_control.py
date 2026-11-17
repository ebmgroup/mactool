"""USB der Phones: lesen (`usb-info`) und ein Phone neu verbinden (`phone-reconnect`).

Für die Devicekontrolle im Dashboard (Andreas 17.11.2026): Phones, die am Kabel hängen, aber
nicht mehr antworten, kommen oft nach kurzem Ab- und Anstecken wieder. Das soll aus der Ferne
gehen — je GENAU EIN Phone, das Dashboard schickt für „mehrere/alle“ je Phone einen Befehl.

Zwei Stufen:
1. `adb -s <serial> reconnect` — der Mac baut die adb-Verbindung neu auf (Strom bleibt dran).
   Geht nur, wenn adb das Phone noch listet (auch als „offline“/„unauthorized“).
2. USB-Port stromlos: `uhubctl -l <hub> -p <port> -a cycle -d <s>` — echtes Ab-/Anstecken.
   Nur an Hubs, die einzelne Ports schalten (ppps). Bei „ganged“ würden ALLE Ports des Hubs
   mit ausgehen → verweigert. Ohne installiertes uhubctl gibt es diese Stufe nicht.

`usb-info` ist rein lesend: USB-Baum aus system_profiler, adb-Liste, uhubctl-Liste (falls da).
Die Zuordnung Phone → Hub/Port wird gemerkt (`usb_ports.json`), damit ein Phone, das schon ganz
aus dem USB-Baum verschwunden ist, trotzdem seinen Port stromlos geschaltet bekommt.

Eingreifendes ist immer `dry=true`, bis ausdrücklich `dry=false` kommt. Kein freier Shell-Befehl.
"""

import json
import logging
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from apk_rollout import RolloutError, _adb

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
PORTS_FILE = APP_DIR / "usb_ports.json"
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
UHUBCTL_PATHS = ("/opt/homebrew/bin/uhubctl", "/usr/local/bin/uhubctl")
BREW_PATHS = ("/opt/homebrew/bin/brew", "/usr/local/bin/brew")
INSTALL_TIMEOUT = 600

ADB_WAIT = 20          # Sekunden nach adb reconnect
POWER_WAIT = 90        # Sekunden nach dem Stromlos-Schalten (Phone meldet sich neu an)
POLL = 3
DEFAULT_OFF_SECONDS = 3
MAX_LIST = 300

# Für Tests austauschbar
_sleep = time.sleep
_monotonic = time.monotonic
_run = subprocess.run


def _serial(serial) -> str:
    serial = str(serial or "").strip()
    if not SERIAL_RE.match(serial):
        raise RolloutError("Genau ein Handy angeben: serial=<seriennummer>")
    return serial


def _exec(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        r = _run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except FileNotFoundError:
        return -2, "nicht gefunden"
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


# ── adb ───────────────────────────────────────────────────────────────


def adb_devices(adb: str) -> dict[str, str]:
    """{serial: zustand} aus `adb devices` — device / offline / unauthorized / …"""
    code, out = _exec([adb, "devices"], timeout=15)
    states = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("List") and not line.startswith("*"):
            states[parts[0]] = parts[1]
    return states


def responds(adb: str, serial: str) -> bool:
    """Wie der Device-Monitor: antwortet das Phone auf `shell echo ok` in 5 s?"""
    code, out = _exec([adb, "-s", serial, "shell", "echo", "ok"], timeout=5)
    return code == 0 and "ok" in out


def _wait_responds(adb: str, serial: str, seconds: int) -> tuple[bool, int]:
    started = _monotonic()
    while _monotonic() - started < seconds:
        _sleep(POLL)
        if responds(adb, serial):
            return True, int(_monotonic() - started)
    return False, int(_monotonic() - started)


# ── uhubctl ───────────────────────────────────────────────────────────


def find_uhubctl() -> str | None:
    found = shutil.which("uhubctl")
    if found:
        return found
    return next((p for p in UHUBCTL_PATHS if Path(p).exists()), None)


_HUB_RE = re.compile(r"^Current status for hub (\S+) \[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})?\s*(.*)\]")
_PORT_RE = re.compile(r"^\s+Port (\d+): (\S+)(.*)$")


def parse_uhubctl(text: str) -> list[dict]:
    """Ausgabe von `uhubctl` → Hubs mit Ports. ppps = einzeln schaltbar, ganged = nur alle."""
    hubs = []
    for line in text.splitlines():
        m = _HUB_RE.match(line)
        if m:
            info = m.group(3)
            hubs.append({
                "location": m.group(1),
                "vid_pid": (m.group(2) or "").lower(),
                "info": info.strip(),
                "ppps": "ppps" in info,
                "ganged": "ganged" in info,
                "ports": [],
            })
            continue
        p = _PORT_RE.match(line)
        if p and hubs:
            rest = p.group(3)
            device = re.search(r"\[(.*)\]", rest)
            dev = device.group(1) if device else ""
            hubs[-1]["ports"].append({
                "port": int(p.group(1)),
                "status": p.group(2),
                "flags": re.sub(r"\[.*\]", "", rest).split(),
                "device": dev,
                # „[04e8:6860 SAMSUNG SAMSUNG_Android R58M…]“: die Seriennummer steht zuletzt
                "serial": dev.split()[-1] if len(dev.split()) >= 2 else None,
            })
    return hubs


def find_brew() -> str | None:
    return shutil.which("brew") or next((p for p in BREW_PATHS if Path(p).exists()), None)


def uhubctl_install(dry: bool = True) -> dict:
    """uhubctl per Homebrew installieren (nur dieses eine Paket, kein freier Name)."""
    existing = find_uhubctl()
    brew = find_brew()
    head = {"uhubctl_vorher": existing, "brew": brew, "plan": [f"{brew or 'brew'} install uhubctl"]}
    if existing:
        return {**head, "status": "unchanged"}
    if not brew:
        return {**head, "status": "refused", "reasons": ["Homebrew ist auf diesem Mac nicht installiert"]}
    if dry:
        return {**head, "status": "dry", "note": "würde uhubctl installieren"}
    import os
    env = {**os.environ, "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_CLEANUP": "1",
           "HOMEBREW_NO_ENV_HINTS": "1", "NONINTERACTIVE": "1"}
    try:
        r = _run([brew, "install", "uhubctl"], capture_output=True, text=True, timeout=INSTALL_TIMEOUT, env=env)
        code, out = r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        code, out = -1, "timeout"
    after = find_uhubctl()
    logger.info(f"uhubctl-install: exit {code}, {after or 'nicht da'}")
    return {**head, "status": "ok" if after else "failed", "exit": code, "ausgabe": out[-1500:],
            "uhubctl_nachher": after, "liste": uhubctl_list(after) if after else None}


def uhubctl_list(path: str | None) -> dict:
    if not path:
        return {"installed": False}
    code, version = _exec([path, "-v"], timeout=10)
    code, out = _exec([path], timeout=30)
    hubs = parse_uhubctl(out)
    for h in hubs:
        h["erzwungen"] = False
    res = {"installed": True, "path": path, "version": version[:40], "hubs": hubs}
    if not hubs:
        # Hubs, die keine Port-Schaltung melden (z. B. Genesys GL3523 auf mac17), listet uhubctl nur
        # mit -f. Die Liste selbst ist auch mit -f rein lesend.
        res["output"] = out[-600:]
        code, out = _exec([path, "-f"], timeout=30)
        forced = parse_uhubctl(out)
        for h in forced:
            h["erzwungen"] = True
        res["hubs"] = forced
        if not forced:
            res["output_f"] = out[-600:]
    return res


# ── Port-Zuordnung merken ─────────────────────────────────────────────


def load_ports() -> dict:
    try:
        return json.loads(PORTS_FILE.read_text())
    except Exception:
        return {}


def remember_ports(hubs: list[dict]) -> dict:
    """Jedes in uhubctl gesehene Phone mit Hub/Port merken (alte Einträge bleiben)."""
    ports = load_ports()
    now = datetime.now().isoformat(timespec="seconds")
    for hub in hubs:
        for port in hub["ports"]:
            if port.get("serial"):
                ports[port["serial"]] = {"location": hub["location"], "port": port["port"],
                                         "ppps": hub["ppps"], "ganged": hub["ganged"],
                                         "erzwungen": hub.get("erzwungen", False),
                                         "vid_pid": hub["vid_pid"], "seen": now}
    try:
        PORTS_FILE.write_text(json.dumps(ports, indent=1, sort_keys=True))
    except OSError as e:
        logger.warning(f"usb_ports.json nicht schreibbar: {e}")
    return ports


# ── system_profiler ───────────────────────────────────────────────────


def _key(item: dict, *needles: str):
    for k, v in item.items():
        kl = k.lower().replace("_", "")
        if any(n in kl for n in needles):
            return v
    return None


def _walk(items, parent, hubs, devices, depth=0):
    for it in items or []:
        if not isinstance(it, dict):
            continue
        children = it.get("_items") or []
        entry = {
            "name": it.get("_name"),
            "vendor_id": _key(it, "vendorid"),
            "product_id": _key(it, "productid"),
            "location_id": _key(it, "locationid"),
            "manufacturer": _key(it, "manufacturer"),
            "serial": _key(it, "serialnum", "serialnumber"),
            "parent": parent,
        }
        # Ebene 0 sind die USB-Busse des Macs, keine Geräte.
        if "hub" in str(entry["name"] or "").lower():
            hubs.append({**entry, "children": len(children)})
        elif depth > 0:
            devices.append(entry)
        _walk(children, entry["name"], hubs, devices, depth + 1)


def usb_tree() -> dict:
    """Hubs und Geräte aus system_profiler (macOS 15 heißt der Typ SPUSBHostDataType)."""
    for typ in ("SPUSBDataType", "SPUSBHostDataType"):
        code, out = _exec(["/usr/sbin/system_profiler", typ, "-json"], timeout=60)
        if code != 0:
            continue
        try:
            data = json.loads(out)
        except ValueError:
            continue
        roots = data.get(typ) or []
        if not roots:
            continue
        hubs, devices = [], []
        _walk(roots, None, hubs, devices)
        return {"quelle": typ, "hubs": hubs[:MAX_LIST], "geraete": devices[:MAX_LIST]}
    return {"quelle": None, "hubs": [], "geraete": []}


# ── Befehle ───────────────────────────────────────────────────────────


def usb_info() -> dict:
    """Nur lesend: USB-Baum, adb-Zustände, uhubctl (Hubs, ppps/ganged, Phone je Port)."""
    adb = _adb()
    tree = usb_tree()
    states = adb_devices(adb)
    hub = uhubctl_list(find_uhubctl())
    if hub.get("hubs"):
        remember_ports(hub["hubs"])
    phones_in_tree = {d["serial"] for d in tree["geraete"] if d.get("serial")}
    return {
        "usb": tree,
        "adb": states,
        "adb_nicht_im_usb_baum": sorted(s for s in states if s not in phones_in_tree),
        "uhubctl": hub,
        "brew": find_brew(),
        "port_zuordnung": load_ports(),
    }


def _port_for(serial: str, hub: dict) -> dict | None:
    for h in hub.get("hubs") or []:
        for p in h["ports"]:
            if p.get("serial") == serial:
                return {"location": h["location"], "port": p["port"], "ppps": h["ppps"], "ganged": h["ganged"],
                        "erzwungen": h.get("erzwungen", False), "quelle": "jetzt"}
    known = load_ports().get(serial)
    return {**known, "quelle": "gemerkt"} if known else None


def phone_reconnect(serial, dry: bool = True, stufe: str = "auto", aus_sekunden=DEFAULT_OFF_SECONDS,
                    now_minutes: int | None = None, erzwingen: bool = False) -> dict:
    """GENAU EIN Phone neu verbinden. stufe: auto (adb, dann Strom) / adb / strom.

    erzwingen=true: auch an Hubs, die keine Port-Schaltung melden (uhubctl -f). Ob dort der Port
    wirklich aus- und wieder angeht, muss je Hub-Typ erst getestet werden — nie Standard.
    """
    from device_monitor import _batch_update_supabase_status
    from ig_worker import _phone_lock
    from phone_control import reboot_blockers

    serial = _serial(serial)
    stufe = str(stufe or "auto").lower()
    if stufe not in ("auto", "adb", "strom"):
        raise RolloutError("stufe muss auto, adb oder strom sein")
    try:
        off = max(2, min(5, int(aus_sekunden)))
    except (TypeError, ValueError):
        raise RolloutError("aus_sekunden muss 2–5 sein")

    adb = _adb()
    listed = adb_devices(adb).get(serial)
    ok_before = responds(adb, serial)
    hub_path = find_uhubctl()
    hub = uhubctl_list(hub_path) if stufe != "adb" else {"installed": bool(hub_path)}
    if hub.get("hubs"):
        remember_ports(hub["hubs"])
    port = _port_for(serial, hub) if stufe != "adb" else None

    reasons, accounts = [], []
    # Ein Phone, das antwortet, kann gerade einen Account fahren — dann nicht dazwischengehen.
    # Antwortet es nicht, läuft dort ohnehin nichts mehr.
    if ok_before:
        reasons, accounts = reboot_blockers(serial, now_minutes)
    lock = _phone_lock(serial)
    if lock.locked():
        reasons.append("auf dem Phone läuft gerade ein Instagram-Update")

    plan = []
    if stufe in ("auto", "adb"):
        plan.append(f"adb -s {serial} reconnect" if listed else "adb reconnect entfällt (adb listet das Phone nicht)")
    power_note = None
    if stufe in ("auto", "strom"):
        if not hub_path:
            power_note = "uhubctl ist auf diesem Mac nicht installiert"
        elif not port:
            power_note = "Port des Phones unbekannt (an keinem schaltbaren Hub gesehen)"
        elif port.get("ganged"):
            power_note = f"Hub {port['location']} schaltet nur alle Ports gemeinsam (ganged)"
        elif (port.get("erzwungen") or not port.get("ppps")) and not erzwingen:
            power_note = f"Hub {port['location']} meldet keine Port-Schaltung — nur mit erzwingen=true (uhubctl -f)"
        else:
            force = port.get("erzwungen") or not port.get("ppps")
            plan.append(f"uhubctl{' -f' if force else ''} -l {port['location']} -p {port['port']} -a cycle -d {off}"
                        + (" (nur falls adb nicht reicht)" if stufe == "auto" else ""))
    head = {
        "serial": serial, "stufe": stufe, "adb_vorher": listed or "nicht gelistet", "antwortet_vorher": ok_before,
        "port": port, "plan": plan, "strom_hinweis": power_note,
        "accounts": [{k: a.get(k) for k in ("username", "runState", "timeSlot")} for a in accounts],
    }
    if reasons:
        return {**head, "status": "refused", "reasons": reasons}
    if stufe == "strom" and power_note:
        return {**head, "status": "refused", "reasons": [power_note]}
    if stufe == "adb" and not listed:
        return {**head, "status": "refused", "reasons": ["adb listet das Phone nicht — nur Stufe strom hilft"]}
    if dry:
        return {**head, "status": "dry", "note": "würde jetzt neu verbinden"}

    if not lock.acquire(blocking=False):
        return {**head, "status": "refused", "reasons": ["Instagram-Update hat das Phone gerade belegt"]}
    steps = []
    ok = False
    try:
        if stufe in ("auto", "adb") and listed:
            code, out = _exec([adb, "-s", serial, "reconnect"], timeout=20)
            ok, secs = _wait_responds(adb, serial, ADB_WAIT)
            steps.append({"schritt": "adb reconnect", "ausgabe": out[-200:], "antwortet": ok, "sekunden": secs})
        if not ok and stufe in ("auto", "strom") and not power_note:
            force = ["-f"] if (port.get("erzwungen") or not port.get("ppps")) else []
            code, out = _exec([hub_path, *force, "-l", str(port["location"]), "-p", str(port["port"]),
                               "-a", "cycle", "-d", str(off)], timeout=30 + off)
            step = {"schritt": f"Port {port['location']}:{port['port']} {off} s stromlos",
                    "exit": code, "ausgabe": out[-400:]}
            if code == 0:
                ok, secs = _wait_responds(adb, serial, POWER_WAIT)
                step.update({"antwortet": ok, "sekunden": secs})
            steps.append(step)
    finally:
        lock.release()

    after = adb_devices(adb).get(serial)
    # Frischen Status sofort melden, nicht erst beim nächsten stündlichen Device-Monitor.
    try:
        _batch_update_supabase_status([serial] if ok else [], [] if ok else [serial])
    except Exception as e:
        logger.debug(f"adb_status nicht gemeldet: {e}")
    logger.info(f"[{serial}] phone-reconnect {stufe}: {'ok' if ok else 'antwortet nicht'}")
    status = "ok" if ok else ("not_tried" if not steps else "still_offline")
    res = {**head, "status": status, "schritte": steps, "adb_nachher": after or "nicht gelistet", "antwortet_nachher": ok}
    if status == "not_tried":
        res["reasons"] = [power_note or "keine Stufe ausführbar"]
    return res
