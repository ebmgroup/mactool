"""Welche Instagram-Versionen auf den Handys liegen — rein lesend.

Vorarbeit für den Instagram-Rollout: Bevor irgendetwas installiert wird, muss
klar sein, was auf jedem Handy liegt. Pro Paket interessiert vor allem:

- Version (versionName/versionCode) — liegt das Handy über, unter oder auf unserer?
- Signatur — ein `adb install -r` behält die Logins nur, wenn sie gleich bleibt.
  Die Kennung ist der Inhalts-Hash des Zertifikats, also über Handys vergleichbar.
- Split-APKs — eine einzelne APK lässt sich nicht über eine geteilte App legen.
- System-App — Samsung liefert Instagram vorinstalliert aus.

Es werden ausschließlich lesende Shell-Befehle geschickt (getprop, pm list,
pm path, dumpsys). Pro Handy zwei adb-Aufrufe, egal wie viele Pakete.
"""

import logging
import re
import shlex
import subprocess

logger = logging.getLogger(__name__)

PACKAGE_PREFIX = "com.instagram."
EXTRA_PACKAGES = ("com.applisto.appcloner",)  # der App Cloner selbst

PROPS = {
    "ro.product.manufacturer": "manufacturer",
    "ro.product.model": "model",
    "ro.build.version.release": "android",
    "ro.build.version.sdk": "sdk",
    "ro.product.cpu.abilist": "abis",
    "ro.miui.ui.version.name": "miui",
    # Xiaomi: "Über USB installieren". Ohne den Schalter lehnt MIUI adb install ab.
    "persist.security.adbinstall": "usb_install",
}

MARK = "@@MACTOOL@@"
ADB_TIMEOUT = 60

_PROP_RE = re.compile(r"^\[([^\]]+)\]: \[(.*)\]$")
# package:<pfad>=<paket> versionCode:<n> installer=<x> — der Pfad darf selbst '='
# enthalten (Base64 in /data/app/~~…==/), enthält aber nie ein Leerzeichen.
_PM_LIST_RE = re.compile(
    r"^package:(?P<path>\S+)=(?P<pkg>[\w.]+)"
    r"(?:\s+versionCode:(?P<code>\d+))?(?:\s+installer=(?P<installer>\S+))?\s*$"
)
_SIG_RE = re.compile(r"signatures=PackageSignatures\{[^}]*?\[([0-9a-fA-F, ]*)\]")


def _run(adb: str, serial: str, script: str) -> str:
    """Ein Shell-Skript auf dem Handy ausführen, stdout zurück."""
    result = subprocess.run(
        [adb, "-s", serial, "shell", script],
        capture_output=True,
        text=True,
        timeout=ADB_TIMEOUT,
    )
    if result.returncode != 0 and not result.stdout:
        raise RuntimeError((result.stderr or f"adb exit {result.returncode}").strip())
    return result.stdout


def _adb_devices(adb: str) -> dict[str, str]:
    """Seriennummer -> Zustand (device, unauthorized, offline …)."""
    out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=15).stdout
    states = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("List"):
            states[parts[0]] = parts[1]
    return states


def _sections(output: str) -> dict[str, str]:
    """Ausgabe an den Markern zerlegen: '@@MACTOOL@@ name' leitet Abschnitt 'name' ein."""
    sections, current = {}, None
    for line in output.splitlines():
        if line.startswith(MARK):
            current = line[len(MARK):].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def parse_props(text: str) -> dict:
    props = {}
    for line in text.splitlines():
        match = _PROP_RE.match(line.strip())
        if match and match.group(1) in PROPS:
            props[PROPS[match.group(1)]] = match.group(2)
    return props


def parse_pm_list(text: str) -> dict[str, dict]:
    """Die relevanten Pakete aus `pm list packages -f -i --show-versioncode`."""
    found = {}
    for line in text.splitlines():
        match = _PM_LIST_RE.match(line.strip())
        if not match:
            continue
        pkg = match.group("pkg")
        if pkg.startswith(PACKAGE_PREFIX) or pkg in EXTRA_PACKAGES:
            installer = match.group("installer")
            found[pkg] = {
                "path": match.group("path"),
                "version_code": int(match.group("code")) if match.group("code") else None,
                "installer": None if installer in (None, "null") else installer,
            }
    return found


def parse_dumpsys(text: str) -> dict:
    """Version, Flags, Signatur und Zeiten aus `dumpsys package <paket>`.

    Bei einer aktualisierten System-App (Samsung) steht unter
    'Hidden system packages:' noch die vorinstallierte Version — die ist das,
    worauf 'Updates deinstallieren' zurückfällt.
    """
    main, _, hidden = text.partition("Hidden system packages:")

    def first(pattern, source=main):
        match = re.search(pattern, source)
        return match.group(1) if match else None

    flags = first(r"pkgFlags=\[([^\]]*)\]") or ""
    signature = _SIG_RE.search(main)
    info = {
        "version_name": first(r"versionName=(\S+)"),
        "version_code": int(first(r"versionCode=(\d+)") or 0) or None,
        "system": "SYSTEM" in flags.split(),
        "updated_system": "UPDATED_SYSTEM_APP" in flags.split(),
        "signature": signature.group(1).replace(" ", "") if signature else None,
        "first_install": first(r"firstInstallTime=([^\n]+)"),
        "last_update": first(r"lastUpdateTime=([^\n]+)"),
    }
    if hidden.strip():
        info["system_version_name"] = first(r"versionName=(\S+)", hidden)
    return info


def read_phone(adb: str, serial: str) -> dict:
    """Ein Handy auslesen. Wirft bei adb-Fehlern."""
    first_pass = _sections(
        _run(
            adb,
            serial,
            f"echo {MARK} props; getprop; "
            # --show-versioncode kennt pm erst ab Android 9 — sonst ohne.
            f"echo {MARK} pm; pm list packages -f -i --show-versioncode 2>/dev/null"
            " || pm list packages -f -i",
        )
    )
    phone = parse_props(first_pass.get("props", ""))
    listed = parse_pm_list(first_pass.get("pm", ""))

    packages = {}
    if listed:
        script = "; ".join(
            f"echo {MARK} path:{pkg}; pm path {shlex.quote(pkg)}; "
            f"echo {MARK} dump:{pkg}; dumpsys package {shlex.quote(pkg)}"
            for pkg in sorted(listed)
        )
        second_pass = _sections(_run(adb, serial, script))
        for pkg, basic in sorted(listed.items()):
            info = parse_dumpsys(second_pass.get(f"dump:{pkg}", ""))
            apks = [
                line for line in second_pass.get(f"path:{pkg}", "").splitlines()
                if line.startswith("package:")
            ]
            info["version_code"] = info["version_code"] or basic["version_code"]
            info["installer"] = basic["installer"]
            info["apk_count"] = len(apks)
            info["split"] = len(apks) > 1
            packages[pkg] = info

    phone["packages"] = packages
    return phone


def _device_names() -> dict[str, str]:
    """Seriennummer -> Name aus der device-Tabelle; leer, wenn nicht erreichbar."""
    try:
        from device_monitor import get_devices_from_db

        return {d["serial"]: d["name"] for d in get_devices_from_db() if d.get("serial")}
    except Exception as e:
        logger.warning(f"Gerätenamen nicht abrufbar: {e}")
        return {}


def _summarize(phones: list[dict]) -> dict:
    """Flottenblick pro Paket: welche Versionen und Signaturen wie oft."""
    summary: dict[str, dict] = {}
    for phone in phones:
        for pkg, info in phone.get("packages", {}).items():
            entry = summary.setdefault(pkg, {"phones": 0, "versions": {}, "signatures": {}})
            entry["phones"] += 1
            version = info.get("version_name") or "?"
            entry["versions"][version] = entry["versions"].get(version, 0) + 1
            signature = info.get("signature") or "?"
            entry["signatures"][signature] = entry["signatures"].get(signature, 0) + 1
    return summary


def inventory(serials: list[str] | None = None) -> dict:
    """Alle (oder die genannten) Handys dieses Macs auslesen."""
    from device_monitor import _find_adb

    adb = _find_adb()
    if not adb:
        return {"verdict": "adb nicht gefunden", "phones": [], "summary": {}}

    states = _adb_devices(adb)
    wanted = serials or sorted(states)
    names = _device_names()

    phones = []
    for serial in wanted:
        entry = {"serial": serial, "name": names.get(serial), "state": states.get(serial, "nicht verbunden")}
        if entry["state"] == "device":
            try:
                entry.update(read_phone(adb, serial))
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
        phones.append(entry)

    summary = _summarize(phones)
    read_ok = sum(1 for p in phones if "packages" in p)
    mixed = sorted(pkg for pkg, s in summary.items() if len(s["signatures"]) > 1)
    verdict = f"{read_ok} von {len(phones)} Handys gelesen"
    if read_ok < len(phones):
        verdict += f", {len(phones) - read_ok} nicht lesbar"
    verdict += (
        f". ACHTUNG: unterschiedliche Signaturen bei {', '.join(mixed)}"
        if mixed else ". Signaturen je Paket einheitlich"
    )
    return {"verdict": verdict, "summary": summary, "phones": phones}
