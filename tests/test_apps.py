"""apps: Instagram-Bestand der Handys aus adb-Ausgaben lesen.

Kein echtes adb: `_run` und `_adb_devices` werden durch Attrappen ersetzt, die
Ausgaben im Format echter Redmi- und Samsung-Handys liefern.
"""
import logging, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

import device_monitor
import phone_apps
import remote_commands

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


M = phone_apps.MARK

REDMI_PROPS = """[ro.product.manufacturer]: [Xiaomi]
[ro.product.model]: [Redmi Note 9]
[ro.build.version.release]: [11]
[ro.build.version.sdk]: [30]
[ro.product.cpu.abilist]: [arm64-v8a,armeabi-v7a,armeabi]
[ro.miui.ui.version.name]: [V125]
[persist.security.adbinstall]: [1]
[ro.irrelevant]: [x]"""

REDMI_PM = """package:/data/app/~~Ab3d==/com.instagram.android-Xy9==/base.apk=com.instagram.android versionCode:372000000 installer=com.android.vending
package:/data/app/~~Q1==/com.instagram.androie-Z2==/base.apk=com.instagram.androie versionCode:372000000 installer=com.applisto.appcloner
package:/data/app/~~Q3==/com.applisto.appcloner-Z4==/base.apk=com.applisto.appcloner versionCode:3201 installer=null
package:/system/app/Settings/Settings.apk=com.android.settings versionCode:30 installer=null"""


def dump(version, code, sig, flags="[ HAS_CODE ALLOW_CLEAR_USER_DATA ]", hidden=None):
    text = f"""Packages:
  Package [pkg] (1a2b3c):
    versionCode={code} minSdk=28 targetSdk=34
    versionName={version}
    pkgFlags={flags}
    primaryCpuAbi=arm64-v8a
    firstInstallTime=2025-01-02 10:00:00
    lastUpdateTime=2026-03-04 11:00:00
    signatures=PackageSignatures{{7d3f1c version:3, signatures:[{sig}], past signatures:[]}}
"""
    if hidden:
        text += f"""
Hidden system packages:
  Package [pkg] (9f9f9f):
    versionCode=1 minSdk=28 targetSdk=30
    versionName={hidden}
    pkgFlags=[ SYSTEM HAS_CODE ]
"""
    return text


def second_pass(entries):
    out = []
    for pkg, (paths, dumped) in entries.items():
        out.append(f"{M} path:{pkg}")
        out.extend(f"package:{p}" for p in paths)
        out.append(f"{M} dump:{pkg}")
        out.append(dumped)
    return "\n".join(out)


REDMI_SECOND = second_pass({
    "com.applisto.appcloner": (["/data/app/x/base.apk"], dump("3.2.1", 3201, "aaaa1111")),
    "com.instagram.android": (
        ["/data/app/x/base.apk", "/data/app/x/split_config.arm64_v8a.apk", "/data/app/x/split_config.xxhdpi.apk"],
        dump("372.0.0.0.10", 372000000, "c0ffee01"),
    ),
    "com.instagram.androie": (["/data/app/y/base.apk"], dump("372.0.0.0.10", 372000000, "beef0001")),
})

# Samsung, Android 8: pm kennt --show-versioncode nicht; Instagram vorinstalliert
# und aktualisiert; die Klon-Signatur weicht vom Redmi ab; alte Signatur-Schreibweise.
SAMSUNG_PROPS = """[ro.product.manufacturer]: [samsung]
[ro.product.model]: [SM-A125F]
[ro.build.version.release]: [8.1.0]
[ro.build.version.sdk]: [27]
[ro.product.cpu.abilist]: [armeabi-v7a,armeabi]"""

SAMSUNG_PM = """package:/data/app/com.instagram.android-1/base.apk=com.instagram.android installer=com.android.vending
package:/data/app/com.instagram.androie-1/base.apk=com.instagram.androie installer=com.applisto.appcloner"""

SAMSUNG_SECOND = second_pass({
    "com.instagram.android": (
        ["/data/app/com.instagram.android-1/base.apk"],
        dump("370.1.0.0.5", 370100000, "c0ffee01",
             flags="[ SYSTEM HAS_CODE UPDATED_SYSTEM_APP ]", hidden="120.0.0.29.118"),
    ),
    "com.instagram.androie": (
        ["/data/app/com.instagram.androie-1/base.apk"],
        dump("370.1.0.0.5", 370100000, "beef0002").replace(
            "PackageSignatures{7d3f1c version:3, signatures:[beef0002], past signatures:[]}",
            "PackageSignatures{4b2c3d0 [beef0002]}",
        ),
    ),
})

PHONES = {
    "c77bd554": (f"{M} props\n{REDMI_PROPS}\n{M} pm\n{REDMI_PM}", REDMI_SECOND),
    "R58N123ABC": (f"{M} props\n{SAMSUNG_PROPS}\n{M} pm\n{SAMSUNG_PM}", SAMSUNG_SECOND),
}
calls = []


def fake_run(adb, serial, script):
    calls.append((serial, script))
    if serial == "kaputt":
        raise RuntimeError("error: device offline")
    first, second = PHONES[serial]
    return first if "getprop" in script else second


phone_apps._run = fake_run
phone_apps._adb_devices = lambda adb: {
    "c77bd554": "device", "R58N123ABC": "device", "kaputt": "device", "fremd": "unauthorized",
}
phone_apps._device_names = lambda: {"c77bd554": "247 Phone - RedmiNote9"}
device_monitor._find_adb = lambda: "/fake/adb"

print("Parser")
listed = phone_apps.parse_pm_list(REDMI_PM)
check("pm list: '=' im Pfad bricht den Paketnamen nicht",
      listed.get("com.instagram.android", {}).get("path") == "/data/app/~~Ab3d==/com.instagram.android-Xy9==/base.apk",
      listed)
check("pm list: fremde Pakete ignoriert", "com.android.settings" not in listed)
check("pm list: installer=null wird None", listed["com.applisto.appcloner"]["installer"] is None)
check("pm list ohne versionCode (Android 8)",
      phone_apps.parse_pm_list(SAMSUNG_PM)["com.instagram.android"]["version_code"] is None)

print("Bestand")
result = phone_apps.inventory()
by_serial = {p["serial"]: p for p in result["phones"]}
redmi, samsung = by_serial["c77bd554"], by_serial["R58N123ABC"]

check("Redmi: Name aus der device-Tabelle", redmi["name"] == "247 Phone - RedmiNote9")
check("Redmi: MIUI und USB-Installation erkannt", redmi.get("miui") == "V125" and redmi.get("usb_install") == "1", redmi)
original = redmi["packages"]["com.instagram.android"]
check("Redmi: Original geteilt in 3 APKs", original["apk_count"] == 3 and original["split"] is True, original)
check("Redmi: Version + Signatur", original["version_name"] == "372.0.0.0.10" and original["signature"] == "c0ffee01", original)
check("Prozessor-Variante gelesen", original.get("abi") == "arm64-v8a", original)
check("Redmi: keine System-App", original["system"] is False and "system_version_name" not in original)
check("Redmi: Klon-Installer ist der App Cloner",
      redmi["packages"]["com.instagram.androie"]["installer"] == "com.applisto.appcloner")

sam_orig = samsung["packages"]["com.instagram.android"]
check("Samsung: aktualisierte System-App erkannt",
      sam_orig["system"] and sam_orig["updated_system"], sam_orig)
check("Samsung: vorinstallierte Version aus 'Hidden system packages'",
      sam_orig.get("system_version_name") == "120.0.0.29.118", sam_orig)
check("Samsung: Version aus dumpsys, wenn pm sie nicht liefert", sam_orig["version_code"] == 370100000)
check("Samsung: alte Signatur-Schreibweise gelesen",
      samsung["packages"]["com.instagram.androie"]["signature"] == "beef0002", samsung["packages"]["com.instagram.androie"])

check("Fehler eines Handys bricht die anderen nicht ab", "offline" in by_serial["kaputt"].get("error", ""), by_serial["kaputt"])
check("unauthorized wird nicht angefasst",
      by_serial["fremd"]["state"] == "unauthorized" and not any(s == "fremd" for s, _ in calls))

summary = result["summary"]
check("Zusammenfassung: Versionen gezählt",
      summary["com.instagram.android"]["versions"] == {"372.0.0.0.10": 1, "370.1.0.0.5": 1}, summary)
check("Urteil meldet abweichende Klon-Signatur",
      "com.instagram.androie" in result["verdict"] and "ACHTUNG" in result["verdict"], result["verdict"])
check("Urteil: Original einheitlich -> nicht gemeldet", "com.instagram.android," not in result["verdict"]
      and not result["verdict"].endswith("com.instagram.android"), result["verdict"])

print("Nur lesend")
scripts = " ".join(s for _, s in calls)
for verb in ("install", "uninstall", "pm clear", "rm ", "reboot", "disable"):
    check(f"kein '{verb}' an die Handys geschickt", verb not in scripts.replace("firstInstall", ""))
check("zwei adb-Aufrufe pro lesbarem Handy", sum(1 for s, _ in calls if s == "c77bd554") == 2)

print("Filter und Fernbefehl")
calls.clear()
only = remote_commands.execute("apps", {"serial": "R58N123ABC"})
check("serial-Filter liest nur dieses Handy",
      [p["serial"] for p in only["phones"]] == ["R58N123ABC"] and {s for s, _ in calls} == {"R58N123ABC"})
check("apps ist ein lesender Befehl", remote_commands.HANDLERS["apps"][1] is False)
check("nicht verbundenes Handy im Filter wird gemeldet",
      phone_apps.inventory(["gibtsnicht"])["phones"][0]["state"] == "nicht verbunden")

print()
if failures:
    print(f"{len(failures)} FEHLGESCHLAGEN")
    sys.exit(1)
print("Alle Prüfungen bestanden.")
