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

_RESULT_RE = re.compile(r"(Success|Failure \[[^\]]*\]|Exception occurred[^\n]*)")


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


# ── apk-fetch ─────────────────────────────────────────────────────────


def fetch(release: str) -> dict:
    """Release aus dem Bucket holen, zusammensetzen, prüfen, APKs auspacken."""
    target = _release_dir(release)
    bucket = _sb().storage.from_(BUCKET)
    manifest = json.loads(bucket.download(f"{release}/manifest.json"))

    target.mkdir(parents=True, exist_ok=True)
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

    for old in target.glob("*.apk"):
        old.unlink()
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
    summary = {
        "release": release,
        "package": info.get("pname"),
        "version_name": info.get("release_version"),
        "version_code": int(info["versioncode"]) if info.get("versioncode") else None,
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
    if before and before.get("version_code") == meta["version_code"] and before.get("signature") == meta["signature"]:
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
        "target": {k: meta[k] for k in ("version_name", "version_code", "signature")},
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
        nodes.append(node)
    return nodes


def screen(serial: str, picture: bool = True) -> dict:
    """Was gerade auf dem Handy zu sehen ist. Ändert nichts am Zustand der Apps."""
    adb = _adb()
    remote = "/data/local/tmp/mactool_ui.xml"
    _shell(adb, serial, "uiautomator", "dump", remote, timeout=30)
    xml = _shell(adb, serial, "cat", remote)
    focus = _shell(adb, serial, "dumpsys window | grep -m1 mCurrentFocus")
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
