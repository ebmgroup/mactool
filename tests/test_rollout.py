"""Rollout: Original-Update auf einem Handy, Entfernen nur im Notfall, nie eine Kopie.

Kein echtes adb und kein Bucket: subprocess und read_phone werden ersetzt.
"""
import json, logging, sys, tempfile, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

import apk_rollout
import phone_apps
import remote_commands

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


print("Grundregeln")
for bad in ("", "a,b", "a b"):
    try:
        apk_rollout._single_serial({"serial": bad})
        check(f"serial '{bad}' abgelehnt", False)
    except apk_rollout.RolloutError:
        check(f"serial '{bad}' abgelehnt", True)
try:
    apk_rollout._release_dir("../etc")
    check("Release-Name mit Pfad abgelehnt", False)
except apk_rollout.RolloutError:
    check("Release-Name mit Pfad abgelehnt", True)
check("apk-original ist eingreifend", remote_commands.HANDLERS["apk-original"][1] is True)
check("apk-fetch ist eingreifend", remote_commands.HANDLERS["apk-fetch"][1] is True)
check("screen ist lesend", remote_commands.HANDLERS["screen"][1] is False)
check("Java-Hash wie Arrays.hashCode", apk_rollout._java_hash(bytes([1, 2, 255])) == format(((31 * (31 * (31 + 1) + 2)) - 1) & 0xFFFFFFFF, "x"))

ui = '<hierarchy><node text="Update" resource-id="com.applisto.appcloner:id/btn" class="android.widget.Button" clickable="true" bounds="[0,0][10,10]" /><node text="" class="android.view.View" clickable="false" bounds="[0,0][1,1]"/></hierarchy>'
nodes = apk_rollout.parse_ui(ui)
check("UI-Abbild: nur ansprechbare Elemente", len(nodes) == 1 and nodes[0]["text"] == "Update" and nodes[0]["click"], nodes)

print("Original installieren")
STAGE = Path(tempfile.mkdtemp())
apk_rollout.STAGE_DIR = STAGE
rel = STAGE / "ig-445"
rel.mkdir()
for name in ("base.apk", "split_config.xxhdpi.apk"):
    (rel / name).write_bytes(b"x")
META = {"package": "com.instagram.android", "version_name": "445", "version_code": 445, "signature": "43c4b509",
        "apks": ["base.apk", "split_config.xxhdpi.apk"]}
(rel / "release.json").write_text(json.dumps(META))

apk_rollout._adb = lambda: "/fake/adb"


def scenario(installed, install_results):
    """installed: Original vorher; install_results: Ergebnisse der install-Aufrufe der Reihe nach."""
    state = {"orig": dict(installed) if installed else None, "shell": [], "installs": 0}
    results = list(install_results)

    def fake_read(adb, serial):
        pkgs = {"com.instagram.androie": {"version_code": 410}}
        if state["orig"]:
            pkgs["com.instagram.android"] = state["orig"]
        return {"packages": pkgs}

    def fake_install(adb, serial, apks):
        state["installs"] += 1
        r = results.pop(0)
        if r == "Success":
            state["orig"] = {"version_code": 445, "version_name": "445", "signature": "43c4b509"}
        return r

    def fake_shell(adb, serial, *cmd, timeout=60):
        state["shell"].append(" ".join(cmd))
        return "Success"

    phone_apps.read_phone = fake_read
    apk_rollout._install = fake_install
    apk_rollout._shell = fake_shell
    import phone_apps as pa
    sys.modules["phone_apps"].read_phone = fake_read
    out = apk_rollout.install_original("R58", "ig-445")
    return out, state


out, st = scenario({"version_code": 449, "signature": "12f09072", "system": True}, ["Success"])
check("Update drüber klappt -> nichts entfernt", st["shell"] == [] and out["verdict"].startswith("OK"), (st, out["verdict"]))

out, st = scenario({"version_code": 449, "signature": "12f09072", "system": True},
                   ["Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE: signatures do not match]", "Success"])
check("Signatur passt nicht -> Updates deinstalliert, dann installiert",
      st["shell"] == ["pm uninstall-system-updates com.instagram.android"] and st["installs"] == 2
      and out["verdict"].startswith("OK"), (st, out))

out, st = scenario({"version_code": 449, "signature": "12f09072", "system": True},
                   ["Failure [INSTALL_FAILED_INSUFFICIENT_STORAGE]"])
check("anderer Fehler -> nichts entfernt, ehrlich gemeldet",
      st["shell"] == [] and "NICHT" in out["verdict"] and "STORAGE" in out["verdict"], (st, out["verdict"]))

out, st = scenario({"version_code": 445, "signature": "43c4b509", "system": True}, [])
check("schon auf Stand -> gar nichts", st["installs"] == 0 and st["shell"] == [], (st, out))

out, st = scenario({"version_code": 449, "signature": "12f09072", "system": False},
                   ["Failure [INSTALL_FAILED_VERSION_DOWNGRADE]", "Success"])
check("Redmi (keine System-App): Original gelöscht und installiert",
      st["shell"] == ["pm uninstall com.instagram.android"] and out["verdict"].startswith("OK"), (st, out))

print("Nie eine Kopie")
import inspect
src = inspect.getsource(apk_rollout)
removals = [line for line in src.splitlines() if "uninstall" in line and "ORIGINAL" not in line
            and not line.strip().startswith(("#", '"', "out =", "if ", "return", "-", "Die", "hart", "Updates", "def"))]
check("jede uninstall-Zeile nennt fest das Original", not removals, removals)
check("apk_stage ist nicht im Repo", "apk_stage/" in (REPO / ".gitignore").read_text())

print()
if failures:
    print(f"{len(failures)} FEHLGESCHLAGEN")
    sys.exit(1)
print("Alle Prüfungen bestanden.")
