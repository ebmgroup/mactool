"""IG Versionen: App-Cloner-Bedienung und Auftragsabwicklung — ohne Handy, ohne Netz.

Der Bildschirm wird als Folge von Abbildern nachgestellt (Texte/IDs wie am 14.10. auf
mac17/428 gesehen). adb-Shell und Supabase sind Attrappen.
"""
import logging, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

import app_cloner
import apk_rollout
import ig_worker
import phone_apps

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


app_cloner.time.sleep = lambda s: None
ig_worker.time.sleep = lambda s: None
apk_rollout._adb = lambda: "/fake/adb"
app_cloner._adb = lambda: "/fake/adb"

# ── Bildschirme ───────────────────────────────────────────────────────


def el(text=None, bounds="[0,0][10,10]", click=False, id=None, desc=None, checked=None):
    e = {"bounds": bounds}
    if text is not None:
        e["text"] = text
    if id:
        e["id"] = id
    if desc:
        e["desc"] = desc
    if click:
        e["click"] = True
    if checked is not None:
        e["checked"] = checked
    return e


MAIN = [el(desc="CLONED APPS    ", click=True, bounds="[375,153][645,243]")]
CLONED = [
    el("​Pending ​updates", "[23,273][518,309]"),
    el("UPDATE ALL", "[518,258][697,324]", click=True),
    el("Instagram • 1", "[23,356][569,446]"), el("UPDATE", "[569,368][697,434]", click=True),
    el("Instagram • 4", "[23,722][569,812]"), el("UPDATE", "[569,734][697,800]", click=True),
    el("​All ​apps", "[23,1344][720,1380]"),
    el("Instagram • 1", "[23,1424][622,1510]"),
]
DIALOG = [el("Update clone", "[105,398][615,445]"),
          el("Safe update", "[90,747][630,807]", click=True, checked=False),
          el("CANCEL", "[356,1084][500,1185]", click=True, id="android:id/button2"),
          el("UPDATE", "[500,1084][637,1185]", click=True, id="android:id/button1")]
CLONING = [el("Cloning Instagram"), el("51%")]
CLONED_OK = [el("App cloned"), el("Instagram was cloned."),
             el("INSTALL APP", "[445,904][637,1005]", click=True, id="android:id/button1")]
PI = "com.android.packageinstaller:id/"
ASK_UPDATE = [el("Do you want to update this app?", id=PI + "install_confirm_question_update"),
              el("Cancel", "[426,850][549,918]", click=True, id=PI + "button2"),
              el("Update", "[549,850][679,918]", click=True, id=PI + "button1")]
ASK_NEW = [el("Do you want to install this app?", id=PI + "install_confirm_question"),
           el("Cancel", "[426,850][549,918]", click=True, id=PI + "button2"),
           el("Install", "[549,850][679,918]", click=True, id=PI + "button1")]
FAILED = [el("App not installed as app isn't compatible with your phone.", id=PI + "install_failed_incompatible"),
          el("Done", "[581,850][679,918]", click=True, id=PI + "button1")]
INSTALLING = [el("Installing…")]


class Phone:
    """Ein Handy: Bildschirmfolge + Versionen. Tippen schaltet den Bildschirm weiter."""

    def __init__(self, screens, versions, after_install=None):
        self.screens = list(screens)
        self.versions = dict(versions)
        self.after_install = after_install or {}
        self.taps, self.shell = [], []

    def current(self):
        return self.screens[0]

    def view(self):
        """Was ein Abbild zeigt. Zwischenbildschirme (Klonen, Installieren) laufen von selbst weiter."""
        shown = self.screens[0]
        if shown in (CLONING, INSTALLING):
            self.advance()
        return shown

    def advance(self):
        if len(self.screens) > 1:
            self.screens.pop(0)

    def on_shell(self, adb, serial, *cmd, timeout=60):
        line = " ".join(cmd)
        self.shell.append(line)
        if line.startswith("input tap"):
            x, y = map(int, line.split()[2:4])
            hit = next((e for e in self.current() if e.get("click") and apk_rollout._center(e["bounds"]) == (x, y)), None)
            self.taps.append((hit or {}).get("text") or (hit or {}).get("desc"))
            if hit and hit.get("id") == PI + "button1" and any(
                    e.get("id") == PI + "install_confirm_question_update" for e in self.current()):
                self.versions.update(self.after_install)
            self.advance()
        elif "dumpsys package" in line and "versionName" in line:
            pkg = line.split()[2]
            return f"    versionName={self.versions[pkg]}" if pkg in self.versions else ""
        elif "topResumedActivity" in line:
            return "topResumedActivity=ActivityRecord{a u0 com.sec.android.app.launcher/.Launcher t1}"
        return ""


def install(phone):
    apk_rollout._shell = phone.on_shell
    app_cloner._shell = phone.on_shell
    app_cloner.screen = lambda serial, picture=False: {"elements": phone.view(), "picture": "apk/screens/x.jpg" if picture else None}


print("Zuordnung Kopie <-> Paket")
check("androie = #1, androil = #8", app_cloner.clone_number("com.instagram.androie") == 1
      and app_cloner.clone_number("com.instagram.androil") == 8)
check("Original ist keine Kopie", not app_cloner.is_clone("com.instagram.android"))
check("fremdes Paket ist keine Kopie", not app_cloner.is_clone("com.whatsapp"))

print("UPDATE-Knopf in der richtigen Zeile")
phone = Phone([CLONED], {})
install(phone)
c = app_cloner.Cloner("R58")
button = c.find_update_button(4)
check("Instagram • 4 -> UPDATE auf Höhe 734–800", button and button["bounds"] == "[569,734][697,800]", button)
check("UPDATE ALL wird nie gewählt", c.find_update_button(1)["bounds"] == "[569,368][697,434]")
check("Kopie ohne Update-Angebot -> None", c.find_update_button(7) is None)

print("Normalablauf")
T = "445.0.0.45.83"
OLD = "410.1.0.63.71"
phone = Phone([MAIN, CLONED, DIALOG, CLONING, CLONED_OK, ASK_UPDATE, INSTALLING],
              {"com.instagram.androih": OLD}, after_install={"com.instagram.androih": T})
install(phone)
out = app_cloner.Cloner("R58").update_clone("com.instagram.androih", T)
check("Kopie #4 aktualisiert", out["result"] == "aktualisiert" and out["version"] == T, out)
check("Tipp-Folge wie von Hand",
      phone.taps == ["CLONED APPS    ", "UPDATE", "UPDATE", "INSTALL APP", "Update"], phone.taps)
check("Phone wird vor jedem Abbild geweckt", sum(s == "input keyevent 224" for s in phone.shell) >= 5
      and "wm dismiss-keyguard" in phone.shell, phone.shell)
check("Installer vorher und nachher beendet (Geister-Fenster)",
      sum("force-stop com.google.android.packageinstaller" in s for s in phone.shell) == 2, phone.shell)
check("App Cloner nie gelöscht/aktualisiert",
      not any("uninstall" in s or "install " in s and "pm" in s for s in phone.shell), phone.shell)

print("App Cloner öffnet direkt im Reiter CLONED APPS (merkt sich den letzten)")
phone = Phone([CLONED, DIALOG, CLONED_OK, ASK_UPDATE, INSTALLING],
              {"com.instagram.androih": OLD}, after_install={"com.instagram.androih": T})
install(phone)
out = app_cloner.Cloner("R58").update_clone("com.instagram.androih", T)
check("ohne Reiter-Tipp weiter", out["result"] == "aktualisiert" and phone.taps[0] == "UPDATE", phone.taps)

print("Installer-Knopf doppelt im Abbild (alle Fenster über uiautomator2)")
ASK_UPDATE_DOUBLE = ASK_UPDATE + [dict(ASK_UPDATE[2])]
phone = Phone([CLONED, DIALOG, CLONED_OK, ASK_UPDATE_DOUBLE, INSTALLING],
              {"com.instagram.androih": OLD}, after_install={"com.instagram.androih": T})
install(phone)
out = app_cloner.Cloner("R58").update_clone("com.instagram.androih", T)
check("gleicher Knopf zweimal -> trotzdem getippt", out["result"] == "aktualisiert" and phone.taps[-1] == "Update", phone.taps)
ASK_UPDATE_TWO = ASK_UPDATE + [el("Update", "[549,1450][679,1518]", click=True, id=PI + "button1")]
c2 = app_cloner.Cloner("R58")
pick = c2.nearest_below(c2.find(ASK_UPDATE_TWO, ids=(PI + "button1",)), ASK_UPDATE_TWO[0])
check("zwei verschiedene -> der direkt unter der Frage", pick["bounds"] == "[549,850][679,918]", pick)

print("Schon aktuell")
phone = Phone([MAIN], {"com.instagram.androih": T})
install(phone)
check("nichts getippt", app_cloner.Cloner("R58").update_clone("com.instagram.androih", T)["result"] == "schon aktuell"
      and phone.taps == [])

print("Android will NEU installieren -> Abbruch")
phone = Phone([MAIN, CLONED, DIALOG, CLONED_OK, ASK_NEW, ASK_NEW], {"com.instagram.androih": OLD})
install(phone)
try:
    app_cloner.Cloner("R58").update_clone("com.instagram.androih", T)
    check("Neuinstallation abgebrochen", False)
except apk_rollout.RolloutError as e:
    check("Neuinstallation abgebrochen", "NEU" in str(e) and "Install" not in phone.taps and "Cancel" in phone.taps, (e, phone.taps))

print("Android lehnt ab -> Meldung")
phone = Phone([MAIN, CLONED, DIALOG, CLONED_OK, ASK_UPDATE, FAILED, FAILED], {"com.instagram.androih": OLD})
install(phone)
try:
    app_cloner.Cloner("R58").update_clone("com.instagram.androih", T)
    check("Ablehnung gemeldet", False)
except apk_rollout.RolloutError as e:
    check("Ablehnung gemeldet", "isn't compatible" in str(e) and phone.taps[-1] == "Done", (e, phone.taps))

print("Safe update angehakt -> wird ausgeschaltet")
dialog_checked = [dict(e) for e in DIALOG]
dialog_checked[1]["checked"] = True
phone = Phone([MAIN, CLONED, dialog_checked, DIALOG, CLONED_OK, ASK_UPDATE, INSTALLING],
              {"com.instagram.androih": OLD}, after_install={"com.instagram.androih": T})
install(phone)
app_cloner.Cloner("R58").update_clone("com.instagram.androih", T)
check("Safe update vor dem Bestätigen abgewählt", phone.taps[2] == "Safe update", phone.taps)

# ── Auftragsabwicklung ────────────────────────────────────────────────

print("Handy: Original zuerst, dann nur veraltete Kopien")
calls = []
ig_worker_phone = {"com.instagram.android": {"version_name": OLD}, "com.instagram.androie": {"version_name": T},
                   "com.instagram.androif": {"version_name": OLD}, "com.applisto.appcloner": {"version_name": "1.5.32"}}
phone_apps.read_phone = lambda adb, serial: {"packages": ig_worker_phone}
apk_rollout.install_original = lambda serial, release: calls.append(("original", serial)) or {"verdict": "OK: Original jetzt 445"}


class FakeCloner:
    def __init__(self, serial, progress=None):
        self.adb = "/fake/adb"

    def foreground(self):
        return "mCurrentFocus=launcher"

    def update_clone(self, package, target):
        calls.append(("clone", package))
        return {"result": "aktualisiert"}


app_cloner.Cloner = FakeCloner
version = {"version_code": 385111388, "release": "r1", "version_name": T}
res = ig_worker.update_phone("R58", version, None, lambda t: None, lambda: False)
check("Reihenfolge: Original, dann androif; androie übersprungen, App Cloner nie",
      calls == [("original", "R58"), ("clone", "com.instagram.androif")], calls)

calls.clear()
ig_worker_phone.update({"com.instagram.android": {"version_name": T}, "com.instagram.androif": {"version_name": T}})
res = ig_worker.update_phone("R58", version, None, lambda t: None, lambda: False)
check("alles aktuell -> nichts angefasst", calls == [] and set(res.values()) == {"schon aktuell"}, (calls, res))

calls.clear()
ig_worker_phone["com.instagram.androif"] = {"version_name": OLD}
ig_worker.update_phone("R58", version, ["com.instagram.androif"], lambda t: None, lambda: False)
check("einzelne Kopie, Original schon aktuell -> nur die Kopie", calls == [("clone", "com.instagram.androif")], calls)

calls.clear()
ig_worker_phone["com.instagram.android"] = {"version_name": OLD}
ig_worker.update_phone("R58", version, ["com.instagram.androif"], lambda t: None, lambda: False)
check("einzelne Kopie, Original veraltet -> Original zuerst",
      calls == [("original", "R58"), ("clone", "com.instagram.androif")], calls)

calls.clear()
ig_worker_phone["com.instagram.android"] = {"version_name": "450.0.0.50.77"}
ig_worker.update_phone("R58", version, ["com.instagram.androif"], lambda t: None, lambda: False)
check("Original NEUER als Standard -> trotzdem erst auf Standard (App Cloner klont aus ihm)",
      calls == [("original", "R58"), ("clone", "com.instagram.androif")], calls)

calls.clear()
ig_worker_phone.update({"com.instagram.androif": {"version_name": T, "version_code": 370711583}})
res = ig_worker.update_phone("R58", version, ["com.instagram.androif"], lambda t: None, lambda: False)
check("Kopie mit gleichem Namen, aber anderem Code (App Cloner 2.7.1) gilt als aktuell",
      calls == [] and res == {"com.instagram.androif": "schon aktuell"}, (calls, res))
ig_worker_phone["com.instagram.android"] = {"version_name": T}


class Busy(FakeCloner):
    def foreground(self):
        return "topResumedActivity=ActivityRecord{a u0 com.instagram.androif/com.instagram.mainactivity.MainActivity t9}"


app_cloner.Cloner = Busy
ig_worker_phone["com.instagram.androif"] = {"version_name": OLD}
ig_worker.BUSY_WAIT_SECONDS = -1
try:
    ig_worker.update_phone("R58", version, None, lambda t: None, lambda: False)
    check("Bot hat Instagram vorne -> Handy übersprungen", False)
except apk_rollout.RolloutError as e:
    check("Bot hat Instagram vorne -> Handy übersprungen", "Vordergrund" in str(e))
app_cloner.Cloner = FakeCloner


class FakeDB:
    """PostgREST-Filter eq./in./is. auf Listen."""

    def __init__(self):
        self.t = {"ig_update_jobs": [], "ig_versions": [], "ig_phones": []}

    def _match(self, row, params):
        for k, v in params.items():
            if k in ("select", "order", "limit", "on_conflict"):
                continue
            if v.startswith("eq.") and str(row.get(k)) != v[3:]:
                return False
            if v.startswith("in.") and str(row.get(k)) not in v[4:-1].split(","):
                return False
            if v == "is.true" and row.get(k) is not True:
                return False
            if v.startswith("lte.") and not (row.get(k) is not None and row[k] <= int(v[4:])):
                return False
        return True

    def select(self, table, params):
        rows = [dict(r) for r in self.t[table] if self._match(r, params)]
        if params.get("order") == "scope.asc,created_at.asc":
            rows.sort(key=lambda r: (r.get("scope", 0), r.get("created_at", r["id"])))
        if params.get("limit"):
            rows = rows[: int(params["limit"])]
        return rows

    def update(self, table, params, values, retries=3):
        hit = [r for r in self.t[table] if self._match(r, params)]
        for r in hit:
            r.update(values)
        return [dict(r) for r in hit]

    def upsert_many(self, table, rows, on_conflict):
        keys = on_conflict.split(",")
        for row in rows:
            old = next((r for r in self.t[table] if all(r[k] == row[k] for k in keys)), None)
            (old.update(row) if old else self.t[table].append(dict(row)))

    def delete(self, table, params):
        self.t[table] = [r for r in self.t[table] if not self._match(r, params)]


print("Auftrag: ganzes Handy")
import config as cfg_mod
cfg_mod.get_config().server_name = "mac17"
db = FakeDB()
db.t["ig_versions"].append({"id": 1, "release": "r1", "version_code": T, "version_name": "445",
                            "status": "ready", "is_standard": True, "sha256": "x"})
db.t["ig_update_jobs"].append({"id": 7, "server_name": "mac17", "serial": "R58", "package": None,
                               "version_id": None, "status": "queued", "scope": 1})
db.t["ig_update_jobs"].append({"id": 8, "server_name": "mac22", "serial": "X", "package": None, "status": "queued", "scope": 1})
ig_worker._ensure_release = lambda v: {}
ig_worker.report_inventory = lambda client, serials=None: 0
calls.clear()
job = ig_worker._claim(db)
check("nur Aufträge dieses Macs", job and job["id"] == 7)
check("zweiter Mac kann denselben Auftrag nicht reservieren", ig_worker._claim(db) is None)
ig_worker.run_job(db, job)
row = db.t["ig_update_jobs"][0]
check("Auftrag done mit Ergebnis je Paket", row["status"] == "done" and "com.instagram.androif" in row["result"]["R58"], row)

print("Auftrag: Abbruch aus dem Dashboard")
db.t["ig_update_jobs"].append({"id": 9, "server_name": "mac17", "serial": "R58", "package": None, "status": "queued", "scope": 1})
ig_worker_phone["com.instagram.android"] = {"version_name": OLD}
ig_worker_phone["com.instagram.androif"] = {"version_name": OLD}
job = ig_worker._claim(db)
db.t["ig_update_jobs"][-1]["status"] = "cancelled"
calls.clear()
ig_worker.run_job(db, job)
check("abgebrochen bleibt abgebrochen, nichts installiert",
      db.t["ig_update_jobs"][-1]["status"] == "cancelled" and calls == [], (db.t["ig_update_jobs"][-1], calls))

print("Reihenfolge: App vor Phone vor Mac, sonst nach Zeit")


def jobrow(id, serial, package, created):
    scope = 2 if serial is None else (1 if package is None else 0)
    return {"id": id, "server_name": "mac17", "serial": serial, "package": package,
            "status": "queued", "scope": scope, "created_at": created}


db.t["ig_update_jobs"] = [jobrow(30, None, None, 1), jobrow(31, "B", None, 2), jobrow(32, "A", "com.instagram.androif", 3)]
check("App-Auftrag zuerst, obwohl zuletzt geklickt", ig_worker._claim(db)["id"] == 32)
check("dann Phone", ig_worker._claim(db)["id"] == 31)
check("Mac zuletzt", ig_worker._claim(db)["id"] == 30)

print("Mac updaten: Bot aus, bis zu 5 Phones parallel, Bot wieder an")
import tempfile, threading as th, time as realtime
import bot_manager
import device_monitor
device_monitor._find_adb = lambda: "/fake/adb"
ig_worker.PAUSE_FILE = Path(tempfile.mkdtemp()) / "ig_bot_pause.json"
bot = {"running": True, "stops": 0, "starts": 0, "start_ok": True}
bot_manager.is_bot_running = lambda: bot["running"]
def _stop():
    bot["stops"] += 1; bot["running"] = False; return True
def _start_bot():
    bot["starts"] += 1
    if bot["start_ok"]:
        bot["running"] = True
    return bot["start_ok"]
bot_manager.stop_bot = _stop
bot_manager.start_bot = _start_bot

active = {"now": 0, "max": 0, "per_serial": {}, "overlap": False, "bot_during": set()}
alock = th.Lock()


def fake_update_phone(serial, version, packages, progress, cancelled):
    with alock:
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        if active["per_serial"].get(serial):
            active["overlap"] = True
        active["per_serial"][serial] = True
        active["bot_during"].add(bot["running"])
        active.setdefault("versions", set()).add(version["id"])
    th.Event().wait(0.05)  # time.sleep ist oben fuer den Test abgeschaltet
    with alock:
        active["now"] -= 1
        active["per_serial"][serial] = False
    return {"x": "aktualisiert"}


ig_worker.update_phone = fake_update_phone
phones12 = {f"P{i:02d}": "device" for i in range(12)}
phones12["UNAUTH"] = "unauthorized"
phone_apps._adb_devices = lambda adb: phones12
db.t["ig_versions"][0]["id"] = 2   # Admin hat inzwischen einen neuen Standard gesetzt
db.t["ig_update_jobs"] = [jobrow(40, None, None, 1)]
ig_worker.run_job(db, ig_worker._claim(db))
row = db.t["ig_update_jobs"][0]
check("alle 12 verbundenen Phones bearbeitet, nicht freigegebene ausgelassen",
      row["status"] == "done" and sorted(row["result"]) == sorted(k for k in phones12 if k != "UNAUTH"), row)
check("mehrere gleichzeitig, aber höchstens 5", 1 < active["max"] <= 5, active["max"])
check("Bot während des Updates aus", active["bot_during"] == {False}, active["bot_during"])
check("Bot danach wieder an, Merkdatei weg",
      bot["running"] and bot["stops"] == 1 and bot["starts"] == 1 and not ig_worker.PAUSE_FILE.exists(), bot)
check("Standard beim Start, nicht beim Klick", active["versions"] == {2} and row["version_id"] == 2, (active, row))

print("Mac updaten bei absichtlich gestopptem Bot: bleibt aus")
bot.update(running=False, stops=0, starts=0)
db.t["ig_update_jobs"] = [jobrow(41, None, None, 1)]
ig_worker.run_job(db, ig_worker._claim(db))
check("kein Stopp, kein Start", bot["stops"] == 0 and bot["starts"] == 0 and not bot["running"], bot)

print("Bot startet nach dem Update nicht -> nächster Durchlauf versucht es erneut")
bot.update(running=True, stops=0, starts=0, start_ok=False)
db.t["ig_update_jobs"] = [jobrow(42, None, None, 1)]
ig_worker.run_job(db, ig_worker._claim(db))
check("Merkdatei bleibt liegen", ig_worker.PAUSE_FILE.exists() and not bot["running"], bot)
bot["start_ok"] = True
ig_worker._recovered = True
ig_worker._last_inventory = realtime.time()
ig_worker._client = lambda: db
ig_worker.run_ig_worker_job()
check("Worker startet den Bot nach, sobald kein Mac-Auftrag mehr offen ist",
      bot["running"] and not ig_worker.PAUSE_FILE.exists(), bot)

print("Einzelne App / Phone: Bot bleibt an")
bot.update(running=True, stops=0, starts=0)
db.t["ig_update_jobs"] = [jobrow(43, "P01", None, 1)]
ig_worker.run_job(db, ig_worker._claim(db))
check("kein Bot-Stopp bei Phone-Klick", bot["stops"] == 0 and active["bot_during"] >= {True}, bot)

print("Zwei Aufträge auf demselben Phone laufen nie gleichzeitig")
active.update(overlap=False, per_serial={})
db.t["ig_update_jobs"] = [jobrow(50, "P01", "com.instagram.androie", 1), jobrow(51, "P01", "com.instagram.androif", 2)]
j1, j2 = ig_worker._claim(db), ig_worker._claim(db)
threads = [th.Thread(target=ig_worker.run_job, args=(db, j)) for j in (j1, j2)]
[x.start() for x in threads]; [x.join() for x in threads]
check("nacheinander auf P01", not active["overlap"] and all(r["status"] == "done" for r in db.t["ig_update_jobs"]),
      (active, db.t["ig_update_jobs"]))

print("Verteiler: bis zur Grenze annehmen, höchstens ein Mac-Auftrag")
started_ids = []
real_start = ig_worker._start
ig_worker._start = lambda job: (ig_worker._active.__setitem__(job["id"], job["scope"]), started_ids.append(job["id"]))
ig_worker._active.clear()
db.t["ig_update_jobs"] = [jobrow(60 + i, f"S{i}", None, i) for i in range(7)] + [jobrow(70, None, None, 0), jobrow(71, None, None, 1)]
ig_worker.dispatch(db)
check("5 Phone-Aufträge + 1 Mac-Auftrag angenommen, Rest wartet",
      sorted(started_ids) == [60, 61, 62, 63, 64, 70], started_ids)
ig_worker._start = real_start
ig_worker._active.clear()

print("Einstellung aus der Ferne nur 1–10")
import remote_commands
for bad in (0, 11):
    try:
        remote_commands._validate("ig_parallel_phones", bad)
        check(f"ig_parallel_phones={bad} abgelehnt", False)
    except remote_commands.CommandError:
        check(f"ig_parallel_phones={bad} abgelehnt", True)

print("Neustart: verwaiste laufende Aufträge werden wieder eingereiht")
db.t["ig_update_jobs"] = [
    {"id": 20, "server_name": "mac17", "status": "running"},
    {"id": 21, "server_name": "mac22", "status": "running"},
    {"id": 22, "server_name": "mac17", "status": "done"},
]
check("nur laufende dieses Macs", ig_worker._recover_orphans(db) == 1
      and [r["status"] for r in db.t["ig_update_jobs"]] == ["queued", "running", "done"], db.t["ig_update_jobs"])

print("Bestand: verschwundene Handys werden entfernt")
db.t["ig_phones"] = [{"server_name": "mac17", "serial": "ALT"}, {"server_name": "mac22", "serial": "ANDERER"}]
phone_apps.inventory = lambda serials=None: {"phones": [{"serial": "R58", "state": "device", "packages": {}}]}
import importlib
real_report = importlib.reload(ig_worker).report_inventory
real_report(db)
serials = sorted((r["server_name"], r["serial"]) for r in db.t["ig_phones"])
check("ALT weg, R58 neu, anderer Mac unberührt", serials == [("mac17", "R58"), ("mac22", "ANDERER")], serials)

print()
if failures:
    print(f"{len(failures)} FEHLGESCHLAGEN")
    sys.exit(1)
print("Alle Prüfungen bestanden.")
