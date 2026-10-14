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
check("phone-log ist lesend", remote_commands.HANDLERS["phone-log"][1] is False)
check("screen ist lesend", remote_commands.HANDLERS["screen"][1] is False)
check("Java-Hash wie Arrays.hashCode", apk_rollout._java_hash(bytes([1, 2, 255])) == format(((31 * (31 * (31 + 1) + 2)) - 1) & 0xFFFFFFFF, "x"))

ui = '<hierarchy><node text="Update" resource-id="com.applisto.appcloner:id/btn" class="android.widget.Button" clickable="true" bounds="[0,0][10,10]" /><node text="" class="android.view.View" clickable="false" bounds="[0,0][1,1]"/></hierarchy>'
nodes = apk_rollout.parse_ui(ui)
check("UI-Abbild: nur ansprechbare Elemente", len(nodes) == 1 and nodes[0]["text"] == "Update" and nodes[0]["click"], nodes)

els = [{"text": "Update", "bounds": "[0,0][10,10]"}, {"text": "Update", "bounds": "[0,20][10,30]"},
       {"text": "Instagram", "bounds": "[0,40][100,60]"}]
check("tap: eindeutiger Treffer", apk_rollout.find_element(els, text="Instagram")["bounds"] == "[0,40][100,60]")
for kwargs, name in (({"text": "Update"}, "mehrdeutig"), ({"text": "Löschen"}, "fehlt")):
    try:
        apk_rollout.find_element(els, **kwargs)
        check(f"tap {name} -> nichts getippt", False)
    except apk_rollout.RolloutError:
        check(f"tap {name} -> nichts getippt", True)
check("tap mehrdeutig mit index", apk_rollout.find_element(els, text="Update", index=1)["bounds"] == "[0,20][10,30]")
check("Mitte der Bounds", apk_rollout._center("[0,40][100,60]") == (50, 50))
check("ui ist eingreifend", remote_commands.HANDLERS["ui"][1] is True)
try:
    apk_rollout.ui("R58", {"action": "rm"})
    check("unbekannte Aktion abgelehnt", False)
except apk_rollout.RolloutError:
    check("unbekannte Aktion abgelehnt", True)

for path, pkg, name in (("/data/x.apk", "com.instagram.androie", "Pfad außerhalb /sdcard"),
                        ("/sdcard/../data/x.apk", "com.instagram.androie", "Pfad mit .."),
                        ("/sdcard/x.apk", "com.instagram.android", "Original statt Kopie"),
                        ("/sdcard/x.apk", "com.whatsapp", "fremdes Paket")):
    try:
        apk_rollout.clone_install("R58", path, pkg)
        check(f"clone-install lehnt ab: {name}", False)
    except apk_rollout.RolloutError:
        check(f"clone-install lehnt ab: {name}", True)
check("clone-install ist eingreifend", remote_commands.HANDLERS["clone-install"][1] is True)
check("phone-apks ist lesend", remote_commands.HANDLERS["phone-apks"][1] is False)

import struct, io


def axml(package, code, name):
    """Minimales binäres Manifest: UTF-8-Stringpool, Ressourcen-IDs, <manifest>-Tag."""
    strs = ["versionCode", "versionName", "package", "manifest", name, package]
    body = b"".join(bytes([len(s), len(s)]) + s.encode() + b"\0" for s in strs)
    offs, o = [], 0
    for s in strs:
        offs.append(o); o += len(s) + 3
    body += b"\0" * (-len(body) % 4)
    hdr = 28
    pool = struct.pack("<HHIIIIII", 1, hdr, hdr + 4 * len(strs) + len(body), len(strs), 0, 0x100,
                       hdr + 4 * len(strs), 0) + struct.pack(f"<{len(strs)}I", *offs) + body
    resmap = struct.pack("<HHI", 0x180, 8, 16) + struct.pack("<II", 0x0101021B, 0x0101021C)
    attrs = (struct.pack("<iiiHBBI", -1, 0, -1, 8, 0, 0x10, code)
             + struct.pack("<iiiHBBI", -1, 1, 4, 8, 0, 0x03, 4)
             + struct.pack("<iiiHBBI", -1, 2, 5, 8, 0, 0x03, 5))
    tag = struct.pack("<HHIIiiiHHHHHH", 0x102, 16, 36 + len(attrs), 1, -1, -1, 3, 20, 20, 3, 0, 0, 0) + attrs
    doc = pool + resmap + tag
    return struct.pack("<HHI", 3, 8, 8 + len(doc)) + doc


fake = STAGE_TMP = Path(tempfile.mkdtemp()) / "x.apk"
with zipfile.ZipFile(fake, "w") as z:
    z.writestr("AndroidManifest.xml", axml("com.instagram.android", 385111388, "445.0.0.45.83"))
check("Manifest: Paket, versionCode, versionName aus der APK",
      apk_rollout.apk_manifest(fake) == {"package": "com.instagram.android", "version_code": 385111388,
                                         "version_name": "445.0.0.45.83"}, apk_rollout.apk_manifest(fake))

print("apk-fetch")
import hashlib
apk_bytes = fake.read_bytes()


class FakeBucket:
    def __init__(self, files):
        self.files = files

    def download(self, path):
        return self.files[path]


class FakeStorage:
    def __init__(self, files):
        self.bucket = FakeBucket(files)

    def from_(self, name):
        return self.bucket


class FakeClient:
    def __init__(self, files):
        self.storage = FakeStorage(files)


def bucket_with(release, file_name, payload):
    half = len(payload) // 2
    parts = [payload[:half], payload[half:]]
    files = {f"{release}/part{i:03d}": p for i, p in enumerate(parts)}
    files[f"{release}/manifest.json"] = json.dumps({
        "file": file_name, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        "parts": [{"name": f"{release}/part{i:03d}", "sha256": hashlib.sha256(p).hexdigest()}
                  for i, p in enumerate(parts)]}).encode()
    return files


apk_rollout.STAGE_DIR = Path(tempfile.mkdtemp())
files = bucket_with("ig-apk", "instagram-445_(1).apk", apk_bytes)
apk_rollout._sb = lambda: FakeClient(files)
(apk_rollout.STAGE_DIR / "ig-apk").mkdir()
(apk_rollout.STAGE_DIR / "ig-apk" / "alt.apk").write_bytes(b"alt")
got = apk_rollout.fetch("ig-apk")
check("einzelne .apk: geladen, nicht weggeräumt",
      got["apks"] == ["instagram-445_(1).apk"] and got["version_code"] == 385111388, got)
check("Reste eines früheren Abrufs weg", not (apk_rollout.STAGE_DIR / "ig-apk" / "alt.apk").exists())
files["ig-apk/part001"] = b"kaputt"
try:
    apk_rollout.fetch("ig-apk")
    check("falsche Prüfsumme bricht ab", False)
except apk_rollout.RolloutError:
    check("falsche Prüfsumme bricht ab", True)

print("Bildschirm-Abbild nie veraltet")
calls_ui = []


def fake_dump_shell(outputs):
    seq = list(outputs)

    def shell(adb, serial, *cmd, timeout=60):
        line = " ".join(cmd)
        calls_ui.append(line)
        if line.startswith("uiautomator dump"):
            return seq.pop(0)
        if line.startswith("cat"):
            return "<hierarchy><node text='frisch' clickable='true' bounds='[0,0][1,1]'/></hierarchy>"
        return ""
    return shell


apk_rollout.time.sleep = lambda s: None
apk_rollout._shell = fake_dump_shell(["ERROR: could not get idle state.", "UI hierchary dumped to: /data/local/tmp/mactool_ui.xml"])
xml = apk_rollout.dump_ui("/fake/adb", "R58")
check("Fehlschlag -> neu versucht, alte Datei vorher gelöscht",
      "frisch" in xml and calls_ui.count("rm -f /data/local/tmp/mactool_ui.xml") == 2, calls_ui)
apk_rollout._shell = fake_dump_shell(["ERROR: could not get idle state."] * 4)
try:
    apk_rollout.dump_ui("/fake/adb", "R58")
    check("dauerhaft unlesbar -> klare Meldung statt alter Datei", False)
except apk_rollout.RolloutError as e:
    check("dauerhaft unlesbar -> klare Meldung statt alter Datei", "idle state" in str(e))

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

out, st = scenario({"version_code": 445, "signature": "12f09072", "system": True}, [])
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
