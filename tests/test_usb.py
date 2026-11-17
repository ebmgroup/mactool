"""usb-info / phone-reconnect ohne echtes adb und uhubctl: Befehle werden von einer Attrappe
beantwortet, die mitschreibt, was ausgeführt worden wäre."""
import logging, subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

import bot_local
import device_monitor
import remote_commands
import usb_control
from apk_rollout import RolloutError

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


class Cfg:
    remote_allow_actions = True
    bot_admin_token = "tok"


remote_commands.get_config = lambda: Cfg
usb_control._adb = lambda: "adb"
usb_control.PORTS_FILE = Path(tempfile.mkdtemp(prefix="usb-")) / "usb_ports.json"
usb_control._sleep = lambda s: None
clock = [0.0]
def tick():
    clock[0] += 3
    return clock[0]
usb_control._monotonic = tick
reported = []
device_monitor._batch_update_supabase_status = lambda on, off: reported.append((list(on), list(off)))
NOON = 12 * 60

UHUB = """Current status for hub 20-1 [2109:2817 VIA Labs, Inc. USB2.0 Hub, USB 2.10, 4 ports, ppps]
  Port 1: 0103 power enable connect [04e8:6860 SAMSUNG SAMSUNG_Android R58M1]
  Port 2: 0100 power
Current status for hub 21-1 [05e3:0610 GenesysLogic USB2.1 Hub, USB 2.10, 4 ports, ganged]
  Port 3: 0103 power enable connect [2717:ff48 Xiaomi Redmi abc123]
"""


class Fake:
    def __init__(self, devices="R58M1\tdevice\n", responds_after=None, uhub=UHUB, uhub_installed=True):
        self.devices = devices
        self.calls = []
        self.responds_after = responds_after   # nach welchem Schritt antwortet das Phone: None/'reconnect'/'cycle'/'immer'
        self.state = "immer" if responds_after == "immer" else None
        self.uhub = uhub
        usb_control.find_uhubctl = (lambda: "/opt/homebrew/bin/uhubctl") if uhub_installed else (lambda: None)

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(cmd)
        out = ""
        if cmd[:2] == ["adb", "devices"]:
            out = "List of devices attached\n" + self.devices
        elif cmd[0] == "adb" and cmd[3:] == ["shell", "echo", "ok"]:
            out = "ok" if self.state in ("immer", "ja") else "error: closed"
            return subprocess.CompletedProcess(cmd, 0 if "ok" == out else 1, out, "")
        elif cmd[0] == "adb" and cmd[-1] == "reconnect":
            if self.responds_after == "reconnect":
                self.state = "ja"
        elif cmd[0].endswith("uhubctl") and "-a" in cmd:
            if self.responds_after == "cycle":
                self.state = "ja"
        elif cmd[0].endswith("uhubctl") and cmd[-1] == "-v":
            out = "2.6.0"
        elif cmd[0].endswith("uhubctl"):
            out = self.uhub
        elif "system_profiler" in cmd[0]:
            out = '{"SPUSBDataType": [{"_name": "USB31Bus", "_items": [{"_name": "USB2.0 Hub", "vendor_id": "0x2109", "_items": [{"_name": "SAMSUNG_Android", "serial_num": "R58M1", "location_id": "0x01110000 / 3"}]}]}]}'
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def ran(self, word):
        return [c for c in self.calls if word in " ".join(c) and "echo" not in c]


def use(fake):
    usb_control._run = fake
    return fake


def accounts(*items):
    bot_local.device_profiles = lambda serial, token=None: [dict(i) for i in items]


accounts({"username": "kaumgesehen", "runState": "finished", "timeSlot": "04:00-05:59 + 16:00-17:59"})

print("parse_uhubctl")
hubs = usb_control.parse_uhubctl(UHUB)
check("zwei Hubs", len(hubs) == 2, hubs)
check("ppps / ganged erkannt", hubs[0]["ppps"] and not hubs[0]["ganged"] and hubs[1]["ganged"] and not hubs[1]["ppps"], hubs)
check("Seriennummer je Port", hubs[0]["ports"][0]["serial"] == "R58M1" and hubs[0]["ports"][1]["serial"] is None, hubs[0])

print("usb-info")
f = use(Fake())
r = remote_commands.execute("usb-info", {})
check("lesend: kein reconnect, kein cycle", not f.ran("reconnect") and not f.ran("-a cycle"), f.calls)
check("Phone im USB-Baum unter dem Hub", r["usb"]["geraete"][0]["serial"] == "R58M1" and r["usb"]["hubs"][0]["name"] == "USB2.0 Hub", r["usb"])
check("Port gemerkt", r["port_zuordnung"]["R58M1"]["port"] == 1 and r["port_zuordnung"]["abc123"]["ppps"] is False, r["port_zuordnung"])
check("usb-info ist lesend", remote_commands.HANDLERS["usb-info"][1] is False)
check("phone-reconnect ist eingreifend", remote_commands.HANDLERS["phone-reconnect"][1] is True)

print("phone-reconnect")
try:
    usb_control.phone_reconnect("R58 M1; reboot")
    check("freie Eingabe verweigert", False)
except RolloutError:
    check("freie Eingabe verweigert", True)

f = use(Fake(responds_after="reconnect"))
r = usb_control.phone_reconnect("R58M1", now_minutes=NOON)
check("ohne dry=false nur Probe", r["status"] == "dry" and not f.ran("reconnect") and not f.ran("-a cycle"), r)
check("Plan nennt beide Stufen", any("reconnect" in p for p in r["plan"]) and any("-p 1 -a cycle -d 3" in p for p in r["plan"]), r["plan"])

reported.clear()
f = use(Fake(responds_after="reconnect"))
r = usb_control.phone_reconnect("R58M1", dry=False, now_minutes=NOON)
check("adb reconnect reicht -> ok, kein Strom", r["status"] == "ok" and f.ran("reconnect") and not f.ran("-a cycle"), r)
check("frischer Status online gemeldet", reported == [(["R58M1"], [])], reported)

f = use(Fake(responds_after="cycle"))
r = usb_control.phone_reconnect("R58M1", dry=False, now_minutes=NOON)
check("adb reicht nicht -> Port stromlos -> ok", r["status"] == "ok" and f.ran("-l 20-1 -p 1 -a cycle -d 3"), r)

f = use(Fake(devices="", responds_after="cycle"))
r = usb_control.phone_reconnect("R58M1", dry=False, now_minutes=NOON)
check("nicht gelistet: gleich Strom (Port aus uhubctl)", r["status"] == "ok" and not f.ran("reconnect") and f.ran("-a cycle"), r)

f = use(Fake(devices="", responds_after="cycle", uhub="No compatible devices detected!\n"))
r = usb_control.phone_reconnect("R58M1", dry=False, now_minutes=NOON)
check("Phone weg aus dem Baum: gemerkter Port", r["status"] == "ok" and r["port"]["quelle"] == "gemerkt" and f.ran("-p 1 -a cycle"), r)

reported.clear()
f = use(Fake(devices="abc123\toffline\n"))
r = usb_control.phone_reconnect("abc123", dry=False, now_minutes=NOON)
check("ganged Hub: kein Strom, nur adb", not f.ran("-a cycle") and r["status"] == "still_offline" and "ganged" in r["strom_hinweis"], r)
check("bleibt offline -> offline gemeldet", reported == [([], ["abc123"])], reported)
r = usb_control.phone_reconnect("abc123", dry=False, stufe="strom", now_minutes=NOON)
check("stufe=strom am ganged Hub -> refused", r["status"] == "refused", r)

f = use(Fake(devices="", uhub_installed=False))
r = usb_control.phone_reconnect("R58M1", dry=False, now_minutes=NOON)
check("weder gelistet noch uhubctl -> not_tried", r["status"] == "not_tried" and not f.ran("reconnect"), r)

accounts({"username": "kaumgesehen", "runState": "running", "timeSlot": "04:00-05:59 + 16:00-17:59"})
f = use(Fake(responds_after="immer"))
r = usb_control.phone_reconnect("R58M1", dry=False, now_minutes=NOON)
check("antwortet + Account läuft -> refused", r["status"] == "refused" and not f.ran("reconnect") and not f.ran("-a cycle"), r)
f = use(Fake(devices="R58M1\toffline\n", responds_after="reconnect"))
r = usb_control.phone_reconnect("R58M1", dry=False, now_minutes=NOON)
check("antwortet nicht -> Account egal, wird verbunden", r["status"] == "ok", r)

try:
    usb_control.phone_reconnect("R58M1", aus_sekunden="x")
    check("aus_sekunden geprüft", False)
except RolloutError:
    check("aus_sekunden geprüft", True)

f = use(Fake(responds_after="cycle"))
accounts()
r = usb_control.phone_reconnect("R58M1", dry=False, stufe="strom", aus_sekunden=9, now_minutes=NOON)
check("aus_sekunden auf 5 begrenzt", f.ran("-d 5") and r["status"] == "ok", r)

print("Hubs ohne Port-Schaltung (nur mit -f)")
NOPS = """Current status for hub 1-1.2 [05e3:0610 GenesysLogic USB2.1 Hub, USB 2.10, 4 ports, nops]
  Port 2: 0103 power enable connect [04e8:6860 SAMSUNG SAMSUNG_Android R58NOPS]
"""
class FakeNops(Fake):
    def __call__(self, cmd, **kw):
        if cmd[0].endswith("uhubctl") and cmd[1:] == []:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "No compatible devices detected!", "")
        if cmd[0].endswith("uhubctl") and cmd[1:] == ["-f"]:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, NOPS, "")
        return super().__call__(cmd, **kw)
accounts()
f = use(FakeNops(devices="R58NOPS\toffline\n", responds_after="cycle"))
info = usb_control.usb_info()
check("Liste mit -f, als erzwungen markiert", info["uhubctl"]["hubs"][0]["erzwungen"] is True and info["port_zuordnung"]["R58NOPS"]["port"] == 2, info["uhubctl"])
check("usb-info schaltet nichts", not f.ran("-a cycle"), f.calls)
r = usb_control.phone_reconnect("R58NOPS", dry=False, now_minutes=NOON)
check("ohne erzwingen kein Strom", not f.ran("-a cycle") and "erzwingen" in r["strom_hinweis"], r)
r = usb_control.phone_reconnect("R58NOPS", dry=False, now_minutes=NOON, erzwingen=True)
check("mit erzwingen: uhubctl -f -l … cycle", r["status"] == "ok" and f.ran("uhubctl -f -l 1-1.2 -p 2 -a cycle -d 3"), (r, f.calls))
f = use(FakeNops(devices="R58NOPS\toffline\n"))
remote_commands.execute("phone-reconnect", {"serial": "R58NOPS", "erzwingen": "true"})
check("Fernbefehl ohne dry=false bleibt Probe, auch mit erzwingen", not f.ran("-a cycle"), f.calls)

print("ganged mit erzwingen")
GANGED = """Current status for hub 2-1.2 [05e3:0610 GenesysLogic USB2.1 Hub, USB 2.10, 4 ports, ganged]
  Port 3: 0503 power highspeed enable connect [04e8:6860 SAMSUNG SAMSUNG_Android NACHBAR1]
  Port 4: 0503 power highspeed enable connect [04e8:6860 SAMSUNG SAMSUNG_Android TESTPHONE]
Current status for hub 2-1 [05e3:0610 GenesysLogic USB2.1 Hub, USB 2.10, 4 ports, ganged]
  Port 1: 0503 power highspeed enable connect [ganged]
"""
check("[ganged] ist keine Seriennummer", [p["serial"] for h in usb_control.parse_uhubctl(GANGED) for p in h["ports"]] == ["NACHBAR1", "TESTPHONE", None])
class FakeGanged(Fake):
    def __init__(self, nachbar_antwortet=True, **kw):
        super().__init__(uhub=GANGED, **kw)
        self.nachbar = nachbar_antwortet
    def __call__(self, cmd, **kw):
        if cmd[0] == "adb" and len(cmd) > 3 and cmd[2] == "NACHBAR1" and cmd[3:] == ["shell", "echo", "ok"]:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0 if self.nachbar else 1, "ok" if self.nachbar else "", "")
        return super().__call__(cmd, **kw)
def profiles_by(mapping):
    bot_local.device_profiles = lambda serial, token=None: [dict(i) for i in mapping.get(serial, [])]
profiles_by({"NACHBAR1": [{"username": "kunde", "runState": "running", "timeSlot": "10:00-11:59"}]})
f = use(FakeGanged(devices="", responds_after="cycle"))
r = usb_control.phone_reconnect("TESTPHONE", dry=False, stufe="strom", now_minutes=NOON)
check("ganged ohne erzwingen -> refused", r["status"] == "refused" and not f.ran("-a cycle"), r)
r = usb_control.phone_reconnect("TESTPHONE", dry=False, stufe="strom", now_minutes=NOON, erzwingen=True)
check("Nachbar fährt Account -> refused", r["status"] == "refused" and r["nachbarn_am_hub"] == ["NACHBAR1"] and not f.ran("-a cycle"), r)
profiles_by({"NACHBAR1": [{"username": "kunde", "runState": "finished", "timeSlot": "20:00-21:59"}]})
r = usb_control.phone_reconnect("TESTPHONE", dry=False, stufe="strom", now_minutes=NOON, erzwingen=True)
check("Nachbar frei -> uhubctl -f am ganged Port", r["status"] == "ok" and f.ran("uhubctl -f -l 2-1.2 -p 4 -a cycle"), r)
profiles_by({"NACHBAR1": [{"username": "kunde", "runState": "running", "timeSlot": "10:00-11:59"}]})
f = use(FakeGanged(nachbar_antwortet=False, devices="", responds_after="cycle"))
r = usb_control.phone_reconnect("TESTPHONE", dry=False, stufe="strom", now_minutes=NOON, erzwingen=True)
check("Nachbar antwortet selbst nicht -> kein Hindernis", r["status"] == "ok", r)
accounts()

print("uhubctl-install")
f = use(Fake(uhub_installed=False))
usb_control.find_brew = lambda: "/opt/homebrew/bin/brew"
r = usb_control.uhubctl_install()
check("ohne dry=false nur Probe", r["status"] == "dry" and not f.ran("brew"), r)
state = {"da": False}
def fake_install(cmd, **kw):
    f.calls.append(cmd)
    state["da"] = True
    return subprocess.CompletedProcess(cmd, 0, "installed", "")
usb_control._run = fake_install
usb_control.find_uhubctl = lambda: "/opt/homebrew/bin/uhubctl" if state["da"] else None
r = usb_control.uhubctl_install(dry=False)
brew = [c for c in f.calls if c[0].endswith("/brew")]
check("installiert genau uhubctl", r["status"] == "ok" and brew == [["/opt/homebrew/bin/brew", "install", "uhubctl"]], (r, brew))
r = usb_control.uhubctl_install(dry=False)
check("schon da -> unchanged", r["status"] == "unchanged", r)
usb_control.find_uhubctl = lambda: None
usb_control.find_brew = lambda: None
check("ohne Homebrew -> refused", usb_control.uhubctl_install(dry=False)["status"] == "refused")
check("uhubctl-install ist eingreifend", remote_commands.HANDLERS["uhubctl-install"][1] is True)

Cfg.remote_allow_actions = False
try:
    remote_commands.execute("phone-reconnect", {"serial": "R58M1"})
    check("gesperrt ohne remote_allow_actions", False)
except remote_commands.CommandError:
    check("gesperrt ohne remote_allow_actions", True)
Cfg.remote_allow_actions = True

print()
print("FAILED: " + ", ".join(failures) if failures else "ALL PASSED")
sys.exit(1 if failures else 0)
