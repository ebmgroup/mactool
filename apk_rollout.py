"""Instagram-Rollout auf die Handys: Datei holen, Original aktualisieren, Bildschirm lesen.

Harte Regeln (mit Andreas abgestimmt):
- Die App-Cloner-Kopien (com.instagram.androie … ) werden NIE gelöscht, nur per
  Update drüberinstalliert. An ihnen hängen die Logins.
- Löschen bzw. "Updates deinstallieren" gibt es nur für das Original
  com.instagram.android — dort liegt kein Bot-Account.
- Eingegriffen wird immer auf genau EINEM Handy pro Auftrag.

Dateien kommen aus dem privaten Bucket `apk`. Supabase nimmt nur ~50 MB pro Objekt,
deshalb liegt jedes Release in Teilen plus `manifest.json` (Name, Größe, sha256 —
gesamt und je Teil). Der Mac setzt zusammen und prüft, bevor irgendetwas
installiert wird.
"""

import hashlib
import json
import logging
import re
import struct
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from config import get_config

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).parent
STAGE_DIR = APP_DIR / "apk_stage"
BUCKET = "apk"
ORIGINAL = "com.instagram.android"
RELEASE_RE = re.compile(r"^[\w.-]+$")
INSTALL_TIMEOUT = 900

# Nur bei diesen Fehlern hilft Entfernen + neu Installieren. Alles andere (Speicher
# voll, USB-Installation gesperrt …) wird gemeldet, ohne etwas zu entfernen.
REMOVE_HELPS = ("INSTALL_FAILED_UPDATE_INCOMPATIBLE", "INSTALL_FAILED_VERSION_DOWNGRADE")

_RESULT_RE = re.compile(r"(Success|Failure \[[^\]]*\]|Exception occurred[^\n]*\n?[^\n]*)")


class RolloutError(RuntimeError):
    """Abbruch mit verständlichem Grund — es wurde nichts (weiter) verändert."""


# ── Hilfen ────────────────────────────────────────────────────────────


def _adb() -> str:
    from device_monitor import _find_adb

    adb = _find_adb()
    if not adb:
        raise RolloutError("adb nicht gefunden")
    return adb


def _single_serial(args: dict) -> str:
    serial = str(args.get("serial") or "").strip()
    if not serial or "," in serial or " " in serial:
        raise RolloutError("Genau ein Handy angeben: serial=<seriennummer>")
    return serial


def _release_dir(release: str) -> Path:
    if not release or not RELEASE_RE.match(release):
        raise RolloutError(f"Ungültiger Release-Name '{release}'")
    return STAGE_DIR / release


def _sb():
    from supabase import create_client

    cfg = get_config()
    return create_client(cfg.supabase_url, cfg.supabase_key)


def _java_hash(data: bytes) -> str:
    """Arrays.hashCode(byte[]) — so bildet Android die Signatur-Kennung in dumpsys."""
    h = 1
    for b in data:
        h = (31 * h + (b - 256 if b > 127 else b)) & 0xFFFFFFFF
    return format(h, "x")


def apk_signature(path: Path) -> str | None:
    """Kennung des ersten Zertifikats aus dem v2/v3-Signaturblock — vergleichbar mit `apps`."""
    data = path.read_bytes()
    eocd = data.rfind(b"PK\x05\x06")
    if eocd < 0:
        return None
    cd_offset = struct.unpack("<I", data[eocd + 16:eocd + 20])[0]
    if data[cd_offset - 16:cd_offset] != b"APK Sig Block 42":
        return None
    size = struct.unpack("<Q", data[cd_offset - 24:cd_offset - 16])[0]
    pos = cd_offset - size - 8 + 8
    while pos < cd_offset - 24:
        length, pair_id = struct.unpack("<QI", data[pos:pos + 12])
        value = data[pos + 12:pos + 8 + length]
        if pair_id in (0xF05368C0, 0x7109871A):  # v3, v2
            signed_len = struct.unpack("<I", value[8:12])[0]
            signed = value[12:12 + signed_len]
            digests_len = struct.unpack("<I", signed[0:4])[0]
            p = 4 + digests_len
            cert_len = struct.unpack("<I", signed[p + 4:p + 8])[0]
            return _java_hash(signed[p + 8:p + 8 + cert_len])
        pos += 8 + length
    return None


_ATTR_VERSION_CODE = 0x0101021B
_ATTR_VERSION_NAME = 0x0101021C


def apk_manifest(path: Path) -> dict:
    """Paket, versionCode und versionName aus dem binären AndroidManifest — ohne aapt.

    Liest nur den ersten Start-Tag (<manifest>): Stringpool, Ressourcen-ID-Tabelle,
    dann dessen Attribute.
    """
    data = zipfile.ZipFile(path).read("AndroidManifest.xml")
    strings, res_ids, pos = [], [], 8
    while pos < len(data):
        chunk_type, header_size, chunk_size = struct.unpack("<HHI", data[pos:pos + 8])
        if chunk_type == 0x0001:  # Stringpool
            count, _, flags, strings_start = struct.unpack("<IIII", data[pos + 8:pos + 24])
            offsets = struct.unpack(f"<{count}I", data[pos + header_size:pos + header_size + 4 * count])
            base = pos + strings_start
            for off in offsets:
                at = base + off
                if flags & 0x100:  # UTF-8: Zeichenlänge, Bytelänge, Bytes
                    at += 2 if data[at] & 0x80 else 1
                    n = data[at] & 0x7F if not data[at] & 0x80 else ((data[at] & 0x7F) << 8) | data[at + 1]
                    at += 2 if data[at] & 0x80 else 1
                    strings.append(data[at:at + n].decode("utf-8", "replace"))
                else:  # UTF-16
                    n = struct.unpack("<H", data[at:at + 2])[0]
                    strings.append(data[at + 2:at + 2 + 2 * n].decode("utf-16-le", "replace"))
        elif chunk_type == 0x0180:  # Ressourcen-IDs der Attributnamen
            res_ids = list(struct.unpack(f"<{(chunk_size - 8) // 4}I", data[pos + 8:pos + chunk_size]))
        elif chunk_type == 0x0102:  # erster Start-Tag = <manifest>
            attr_start, attr_size, attr_count = struct.unpack("<HHH", data[pos + 24:pos + 30])
            info = {}
            for i in range(attr_count):
                at = pos + 16 + attr_start + i * attr_size
                _, name, raw, _, _, vtype, value = struct.unpack("<iiiHBBI", data[at:at + 20])
                rid = res_ids[name] if name < len(res_ids) else None
                text = strings[raw] if raw >= 0 else None
                if rid == _ATTR_VERSION_CODE:
                    info["version_code"] = value
                elif rid == _ATTR_VERSION_NAME:
                    info["version_name"] = text
                elif strings[name] == "package":
                    info["package"] = text
            return info
        pos += chunk_size
    return {}


# ── apk-fetch ─────────────────────────────────────────────────────────


def fetch(release: str) -> dict:
    """Release aus dem Bucket holen, zusammensetzen, prüfen, APKs auspacken."""
    target = _release_dir(release)
    bucket = _sb().storage.from_(BUCKET)
    manifest = json.loads(bucket.download(f"{release}/manifest.json"))

    target.mkdir(parents=True, exist_ok=True)
    # Reste eines früheren Abrufs zuerst weg — danach nicht mehr, sonst trifft es bei
    # einer einzelnen .apk die gerade geladene Datei selbst.
    for old in target.glob("*.apk"):
        old.unlink()
    bundle = target / Path(manifest["file"]).name
    whole = hashlib.sha256()
    with open(bundle, "wb") as out:
        for part in manifest["parts"]:
            data = bucket.download(part["name"])
            if hashlib.sha256(data).hexdigest() != part["sha256"]:
                raise RolloutError(f"Prüfsumme falsch: {part['name']}")
            whole.update(data)
            out.write(data)
    if whole.hexdigest() != manifest["sha256"]:
        bundle.unlink(missing_ok=True)
        raise RolloutError("Prüfsumme der Gesamtdatei falsch")

    info = {}
    if bundle.suffix == ".apk":
        apks = [bundle]
    else:  # .apkm / .apks / .xapk: ZIP mit base.apk + split_*.apk
        with zipfile.ZipFile(bundle) as archive:
            for name in archive.namelist():
                if name.endswith(".apk") and "/" not in name:
                    archive.extract(name, target)
            if "info.json" in archive.namelist():
                info = json.loads(archive.read("info.json"))
        apks = sorted(target.glob("*.apk"))

    base = next((a for a in apks if a.name in ("base.apk", bundle.name)), apks[0] if apks else None)
    if base is None:
        raise RolloutError("Keine APK im Paket")
    manifest_info = apk_manifest(base)  # die Datei selbst ist die Wahrheit, nicht info.json
    summary = {
        "release": release,
        "package": manifest_info.get("package") or info.get("pname"),
        "version_name": manifest_info.get("version_name") or info.get("release_version"),
        "version_code": manifest_info.get("version_code")
        or (int(info["versioncode"]) if info.get("versioncode") else None),
        "variant": info.get("variant"),
        "signature": apk_signature(base),
        "apks": [a.name for a in apks],
        "bytes": manifest["bytes"],
        "sha256": manifest["sha256"],
    }
    (target / "release.json").write_text(json.dumps(summary, indent=2))
    return summary


def _load_release(release: str) -> tuple[dict, list[Path]]:
    target = _release_dir(release)
    meta_file = target / "release.json"
    if not meta_file.exists():
        raise RolloutError(f"Release '{release}' ist nicht geholt — erst apk-fetch")
    meta = json.loads(meta_file.read_text())
    apks = [target / name for name in meta["apks"]]
    missing = [a.name for a in apks if not a.exists()]
    if missing:
        raise RolloutError(f"Dateien fehlen: {', '.join(missing)} — apk-fetch wiederholen")
    return meta, apks


# ── Original installieren ─────────────────────────────────────────────


def _shell(adb: str, serial: str, *command: str, timeout: int = 60) -> str:
    result = subprocess.run(
        [adb, "-s", serial, "shell", *command], capture_output=True, text=True, timeout=timeout
    )
    return (result.stdout + result.stderr).strip()


def _install(adb: str, serial: str, apks: list[Path]) -> str:
    """Update drüber (-r). Liefert 'Success' oder die Failure-Zeile."""
    command = [adb, "-s", serial, "install-multiple" if len(apks) > 1 else "install", "-r"]
    result = subprocess.run(
        command + [str(a) for a in apks], capture_output=True, text=True, timeout=INSTALL_TIMEOUT
    )
    text = (result.stdout + "\n" + result.stderr).strip()
    match = _RESULT_RE.search(text)
    return match.group(1) if match else (text[-300:] or f"exit {result.returncode}")


def _remove_original(adb: str, serial: str, is_system: bool) -> str:
    """Nur für das Original: Updates deinstallieren (System-App) bzw. löschen.

    Die einzige Stelle im Tool, die etwas von einem Handy entfernt. Das Paket ist
    hart verdrahtet — keine Kopie kann hier je ankommen.
    """
    if is_system:
        out = _shell(adb, serial, "pm", "uninstall-system-updates", ORIGINAL)
        if "Success" in out:
            return "Updates deinstalliert"
        # Ältere pm-Versionen: uninstall auf einer System-App entfernt nur die Updates.
        out = _shell(adb, serial, "pm", "uninstall", ORIGINAL)
        return "Updates deinstalliert (pm uninstall)" if "Success" in out else f"fehlgeschlagen: {out[-200:]}"
    out = _shell(adb, serial, "pm", "uninstall", ORIGINAL)
    return "gelöscht" if "Success" in out else f"fehlgeschlagen: {out[-200:]}"


def install_original(serial: str, release: str) -> dict:
    """Original-Instagram auf genau einem Handy auf das Release bringen."""
    from phone_apps import read_phone

    meta, apks = _load_release(release)
    if meta.get("package") != ORIGINAL:
        raise RolloutError(f"Release ist {meta.get('package')}, nicht {ORIGINAL}")
    adb = _adb()

    before = read_phone(adb, serial)["packages"].get(ORIGINAL)
    steps = []
    # Nur die Version zählt: Meta signiert mit Schlüssel-Rotation (v3), das Handy zeigt
    # danach eine andere Kennung als die erste im Paket (445 auf 428: 43c4b509 -> 12f09072).
    if before and before.get("version_code") == meta["version_code"]:
        return {"serial": serial, "verdict": "schon auf diesem Stand, nichts getan", "before": before}

    result = _install(adb, serial, apks)
    steps.append({"step": "update drüber", "result": result})

    if before is not None and any(code in result for code in REMOVE_HELPS):
        steps.append({"step": "Original entfernen", "result": _remove_original(adb, serial, before.get("system", False))})
        time.sleep(2)
        result = _install(adb, serial, apks)
        steps.append({"step": "installieren", "result": result})

    after = read_phone(adb, serial)["packages"].get(ORIGINAL)
    ok = bool(after) and after.get("version_code") == meta["version_code"]
    return {
        "serial": serial,
        "verdict": (
            f"OK: Original jetzt {after.get('version_name')}" if ok
            else f"NICHT aktualisiert — letzter Schritt: {result}"
        ),
        "target": {k: meta[k] for k in ("version_name", "version_code")},
        "before": before,
        "steps": steps,
        "after": after,
    }


# ── Bildschirm lesen ──────────────────────────────────────────────────

_NODE_RE = re.compile(r"<node ([^>]*?)/?>")
_ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def parse_ui(xml: str) -> list[dict]:
    """Die sichtbaren, ansprechbaren Elemente aus einem uiautomator-Abbild."""
    nodes = []
    for match in _NODE_RE.finditer(xml):
        attrs = dict(_ATTR_RE.findall(match.group(1)))
        text, desc, rid = attrs.get("text", ""), attrs.get("content-desc", ""), attrs.get("resource-id", "")
        clickable = attrs.get("clickable") == "true"
        if not (text or desc or clickable):
            continue
        node = {"bounds": attrs.get("bounds"), "class": attrs.get("class", "").rsplit(".", 1)[-1]}
        for key, value in (("text", text), ("desc", desc), ("id", rid)):
            if value:
                node[key] = value
        if clickable:
            node["click"] = True
        if attrs.get("checkable") == "true":
            node["checked"] = attrs.get("checked") == "true"
        nodes.append(node)
    return nodes


def screen(serial: str, picture: bool = True) -> dict:
    """Was gerade auf dem Handy zu sehen ist. Ändert nichts am Zustand der Apps."""
    adb = _adb()
    remote = "/data/local/tmp/mactool_ui.xml"
    _shell(adb, serial, "uiautomator", "dump", remote, timeout=30)
    xml = _shell(adb, serial, "cat", remote)
    focus = _shell(adb, serial, "dumpsys activity activities | grep -m1 -E 'topResumedActivity|mResumedActivity'")
    result = {"serial": serial, "focus": focus, "elements": parse_ui(xml)}

    if picture:
        cfg = get_config()
        with tempfile.TemporaryDirectory() as tmp:
            png, jpg = Path(tmp) / "s.png", Path(tmp) / "s.jpg"
            png.write_bytes(subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"],
                                           capture_output=True, timeout=30).stdout)
            subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "960", str(png), "--out", str(jpg)],
                           capture_output=True, timeout=30)
            path = f"screens/{cfg.server_name}/{serial}-{int(time.time())}.jpg"
            _sb().storage.from_(BUCKET).upload(
                path, jpg.read_bytes(), file_options={"content-type": "image/jpeg", "x-upsert": "true"}
            )
            result["picture"] = f"{BUCKET}/{path}"
    return result


def phone_log(serial: str, pattern: str | None, lines: int = 200) -> dict:
    """Ende des Systemprotokolls (logcat) eines Handys, optional gefiltert. Nur lesend."""
    adb = _adb()
    lines = max(1, min(int(lines), 1000))
    # Ganzer Puffer aller Logs: der Grund einer abgelehnten Installation steht im
    # system-Puffer und ist im main-Puffer nach zwei Minuten schon herausgerollt.
    out = subprocess.run([adb, "-s", serial, "logcat", "-d", "-b", "all"],
                         capture_output=True, text=True, timeout=120).stdout
    matcher = re.compile(pattern, re.IGNORECASE) if pattern else None
    hits = [l[:400] for l in out.splitlines() if not matcher or matcher.search(l)]
    return {"serial": serial, "grep": pattern, "lines": hits[-lines:]}


# ── Vom App Cloner gebaute Kopien ─────────────────────────────────────

SHARED_ROOT = "/sdcard/"
_APK_LINE_RE = re.compile(r"^(\d+)\s+(\d+)\s+(/sdcard/.+\.apk)$")


def phone_apks(serial: str) -> dict:
    """APK-Dateien im gemeinsamen Speicher des Handys (dort legt der App Cloner seine ab)."""
    adb = _adb()
    out = _shell(adb, serial, f"find {SHARED_ROOT} -iname '*.apk' -type f -exec stat -c '%s %Y %n' {{}} + 2>/dev/null",
                 timeout=120)
    files = []
    for line in out.splitlines():
        match = _APK_LINE_RE.match(line.strip())
        if match:
            files.append({"path": match.group(3), "bytes": int(match.group(1)),
                          "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(match.group(2))))})
    files.sort(key=lambda f: f["modified"], reverse=True)
    return {"serial": serial, "files": files}


def clone_install(serial: str, path: str, package: str) -> dict:
    """Eine vom App Cloner gebaute Kopie per Update drüberinstallieren.

    Nur `install -r` — keine Neuinstallation, kein Löschen. Die Kopie muss schon auf
    dem Handy sein, sonst wird abgebrochen.
    """
    from phone_apps import read_phone

    if not path.startswith(SHARED_ROOT) or not path.endswith(".apk") or ".." in path or "'" in path:
        raise RolloutError(f"Pfad '{path}' nicht erlaubt (nur {SHARED_ROOT}…/x.apk)")
    if not package.startswith("com.instagram.") or package == ORIGINAL:
        raise RolloutError("package muss eine Kopie sein (com.instagram.androie …)")
    adb = _adb()
    before = read_phone(adb, serial)["packages"].get(package)
    if before is None:
        raise RolloutError(f"{package} ist auf {serial} nicht installiert — nur Updates erlaubt")

    # pm darf /sdcard auf neueren Android-Versionen nicht lesen -> erst auf den Mac holen.
    # Die Datei liegt danach ohnehin dort, wo das Verteilen sie braucht.
    local = STAGE_DIR / "clones" / f"{package}-{Path(path).name}"
    local.parent.mkdir(parents=True, exist_ok=True)
    pulled = subprocess.run([adb, "-s", serial, "pull", path, str(local)],
                            capture_output=True, text=True, timeout=INSTALL_TIMEOUT)
    if pulled.returncode != 0 or not local.exists():
        raise RolloutError(f"adb pull fehlgeschlagen: {(pulled.stderr or pulled.stdout)[-200:]}")
    result = _install(adb, serial, [local])
    after = read_phone(adb, serial)["packages"].get(package)
    return {
        "serial": serial,
        "package": package,
        "verdict": (f"OK: {package} jetzt {after.get('version_name')}" if result == "Success"
                    else f"NICHT aktualisiert: {result}"),
        "local_file": str(local),
        "signature": apk_signature(local),
        "before": before,
        "after": after,
    }


# ── App Cloner bedienen ───────────────────────────────────────────────

APP_CLONER = "com.applisto.appcloner"


def _center(bounds: str) -> tuple[int, int]:
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", bounds))
    return (x1 + x2) // 2, (y1 + y2) // 2


def find_element(elements: list[dict], text: str | None = None, desc: str | None = None,
                 rid: str | None = None, index: int | None = None) -> dict:
    """Genau ein Element nach Text / Beschreibung / Resource-ID — sonst Abbruch."""
    hits = [
        e for e in elements
        if (text is None or e.get("text") == text)
        and (desc is None or e.get("desc") == desc)
        and (rid is None or e.get("id") == rid)
    ]
    if index is not None and 0 <= index < len(hits):
        return hits[index]
    if len(hits) != 1:
        raise RolloutError(
            f"{len(hits)} Treffer für text={text!r} desc={desc!r} id={rid!r} — "
            "nichts getippt" + (" (index=<n> wählt einen)" if hits else "")
        )
    return hits[0]


def ui(serial: str, args: dict) -> dict:
    """Eine Bedienaktion auf genau einem Handy, danach der neue Bildschirm.

    open   — startet ausschließlich den App Cloner
    tap    — tippt ein Element, das im aktuellen Abbild eindeutig gefunden wird
    scroll — dir=down|up;  back / home — Systemtasten
    """
    action = str(args.get("action") or "")
    if action not in ("open", "tap", "scroll", "back", "home"):
        raise RolloutError("action muss open, tap, scroll, back oder home sein")
    adb = _adb()
    done = action
    if action == "open":
        _shell(adb, serial, "monkey", "-p", APP_CLONER, "-c", "android.intent.category.LAUNCHER", "1")
    elif action == "tap":
        current = screen(serial, picture=False)["elements"]
        index = args.get("index")
        element = find_element(current, args.get("text"), args.get("desc"), args.get("id"),
                               int(index) if index is not None else None)
        x, y = _center(element["bounds"])
        _shell(adb, serial, "input", "tap", str(x), str(y))
        done = f"tap {element.get('text') or element.get('desc') or element.get('id')} @ {x},{y}"
    elif action == "scroll":
        down = str(args.get("dir") or "down") == "down"
        _shell(adb, serial, "input", "swipe", "360", "1200" if down else "500", "360", "500" if down else "1200", "400")
    elif action == "back":
        _shell(adb, serial, "input", "keyevent", "4")
    else:  # home
        _shell(adb, serial, "input", "keyevent", "3")

    time.sleep(float(args.get("wait") or 2))
    result = screen(serial, picture=str(args.get("bild", "true")).lower() != "false")
    result["done"] = done
    return result
