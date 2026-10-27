"""Dashboard-Automatik: bot-config, phone-info, samsung-updates, phone-reboot.

Keine Bot-App und kein adb: die HTTP- bzw. Shell-Aufrufe werden durch Attrappen ersetzt,
die mitschreiben, was gesendet worden wäre.
"""
import copy, logging, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

import bot_local
import phone_control
import remote_commands
from apk_rollout import RolloutError

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


class Cfg:
    bot_admin_token = "tok"
    remote_allow_actions = True


bot_local.get_config = lambda: Cfg
remote_commands.get_config = lambda: Cfg


# ── bot-config ────────────────────────────────────────────────────────

def make_profile(follow="40-50", run="finished", slot="04:00-05:59 + 16:00-17:59", targets="", stop="25"):
    return {"id": "24", "username": "kaumgesehen", "platform": "instagram", "runState": run,
            "sessionStatus": "good", "timeSlot": slot, "source": "hashtag-posts-top",
            "details": {"config": {
                "follow-input": {"value": follow, "params": {"follow-limit": follow, "total-follows-limit": follow,
                                                             "follow-percentage": "100"}},
                "unfollow-input": {"value": "40-50", "params": {"unfollow": "40-50", "total-unfollows-limit": "40-50"}},
                "stop-unfollow-after": stop, "blogger-followers": targets}}}


class FakeBot:
    """Bot-App-Attrappe: GET liefert den Zustand, PATCH ändert ihn wie config.save/targeting.save."""

    def __init__(self, profile, enabled=True, revert=False):
        self.profile = profile
        self.enabled = enabled
        self.revert = revert          # Supabase-Workaround: dreht Follows sofort zurück
        self.patches = []

    def get(self, path, token=None, header=None):
        assert token == "tok", "Token fehlt"
        return 200, {"ok": True, "profile": copy.deepcopy(self.profile)}

    def targeting(self, profile_id):
        return {"value": self.profile["details"]["config"]["blogger-followers"], "enabled": self.enabled}

    def patch(self, path, token, payload):
        self.patches.append((path, payload))
        cfg = self.profile["details"]["config"]
        if path.endswith("/targeting"):
            src = payload["sources"]["blogger-followers"]
            cfg["blogger-followers"] = src["value"]
            self.enabled = src["enabled"]
        elif not self.revert:
            p, v = payload["parameter"], payload["value"]
            if p == "total-follows-limit":
                cfg["follow-input"]["value"] = v
                cfg["follow-input"]["params"]["follow-limit"] = v
                cfg["follow-input"]["params"]["total-follows-limit"] = v
            elif p == "stop-unfollow-after":
                cfg["stop-unfollow-after"] = v
        return 200, {"ok": True}


def use(bot):
    bot_local.get = bot.get
    bot_local.targeting_state = bot.targeting
    bot_local._patch = bot.patch
    return bot


NOON = 12 * 60   # 12:00 — außerhalb 04-06/16-18

print("bot-config: Werte prüfen")
for param, value in (("total-follows-limit", "81"), ("total-follows-limit", "30-20"), ("total-follows-limit", "abc"),
                     ("total-follows-limit", "20-25-30"), ("stop-unfollow-after", "61"), ("stop-unfollow-after", "1-61"),
                     ("follow-percentage", "50"), ("blogger-followers", "Gross"), ("blogger-followers", "a b"),
                     ("blogger-followers", "x" * 31), ("total-follows-limit", True)):
    try:
        bot_local.validate_config_value(param, value)
        check(f"{param}={value!r} abgelehnt", False)
    except bot_local.BotLocalError:
        check(f"{param}={value!r} abgelehnt", True)
check("Zahl aus macctl (20) wird Text", bot_local.validate_config_value("stop-unfollow-after", 20) == "20")
check("80 ist erlaubt", bot_local.validate_config_value("total-unfollows-limit", "70-80") == "70-80")
check("Liste geglättet, Doppelte weg",
      bot_local.validate_config_value("blogger-followers", " a.b ,c_d,a.b,") == "a.b,c_d")
check("leere Liste erlaubt (Rückweg)", bot_local.validate_config_value("blogger-followers", "") == "")

print("bot-config: Schutz")
for pid in ("all", "new", "*", "1;2", ""):
    try:
        bot_local.bot_config(pid, "total-follows-limit", "20-25", "40-50", now_minutes=NOON)
        check(f"profile={pid!r} abgelehnt", False)
    except bot_local.BotLocalError:
        check(f"profile={pid!r} abgelehnt", True)
try:
    remote_commands._cmd_bot_config({"profile": "24", "param": "total-follows-limit", "value": "20-25"})
    check("ohne expect abgelehnt", False)
except remote_commands.CommandError:
    check("ohne expect abgelehnt", True)

bot = use(FakeBot(make_profile(run="running")))
r = bot_local.bot_config("24", "total-follows-limit", "20-25", "40-50", dry=False, now_minutes=NOON)
check("läuft -> refused, kein PATCH", r["status"] == "refused" and not bot.patches, r)

bot = use(FakeBot(make_profile()))
r = bot_local.bot_config("24", "total-follows-limit", "20-25", "30-40", dry=False, now_minutes=NOON)
check("expect passt nicht -> refused, kein PATCH", r["status"] == "refused" and "expect" in r["reason"]
      and not bot.patches, r)

p = make_profile(); p["details"]["config"]["follow-input"]["params"]["follow-limit"] = "35"
bot = use(FakeBot(p))
r = bot_local.bot_config("24", "total-follows-limit", "20-25", "40-50", dry=False, now_minutes=NOON)
check("drei Follow-Felder ungleich -> refused", r["status"] == "refused" and "einheitlich" in r["reason"]
      and not bot.patches, r)

bot = use(FakeBot(make_profile()))
r = bot_local.bot_config("24", "total-follows-limit", "20-25", "40-50", dry=False, now_minutes=3 * 60 + 57)
check("Zeitfenster beginnt gleich -> refused", r["status"] == "refused" and not bot.patches, r)

print("bot-config: dry und echt")
bot = use(FakeBot(make_profile(slot="run manually")))   # echte Uhr: darf nie "gleich Slot" sein
r = remote_commands._cmd_bot_config({"profile": 24, "param": "total-follows-limit", "value": "20-25",
                                     "expect": "40-50"})
check("Standard ist dry: nur Plan", r["status"] == "dry" and not bot.patches
      and r["plan"]["path"] == "/api/profiles/24/config"
      and r["plan"]["body"] == {"parameter": "total-follows-limit", "value": "20-25"}, r)

r = bot_local.bot_config("24", "total-follows-limit", "20-25", "40-50", dry=False, now_minutes=NOON)
check("echt: ein PATCH, alle drei Felder neu -> ok",
      r["status"] == "ok" and len(bot.patches) == 1 and set(r["after"].values()) == {"20-25"}
      and set(r["before"].values()) == {"40-50"}, r)

bot = use(FakeBot(make_profile(), revert=True))
r = bot_local.bot_config("24", "total-follows-limit", "20-25", "40-50", dry=False, now_minutes=NOON)
check("Bot-App dreht zurück -> reverted_or_failed mit vorher/nachher",
      r["status"] == "reverted_or_failed" and set(r["after"].values()) == {"40-50"}, r)

bot = use(FakeBot(make_profile()))
r = bot_local.bot_config("24", "stop-unfollow-after", 20, 25, dry=False, now_minutes=NOON)
check("stop-unfollow-after: Zahlen aus macctl, ok", r["status"] == "ok" and r["after"] == {"stop-unfollow-after": "20"}, r)

r = bot_local.bot_config("24", "stop-unfollow-after", "20", "20", dry=False, now_minutes=NOON)
check("schon so -> unchanged, kein PATCH", r["status"] == "unchanged" and len(bot.patches) == 1, r)

print("bot-config: blogger-followers")
bot = use(FakeBot(make_profile(targets=""), enabled=False))
r = bot_local.bot_config("24", "blogger-followers", "testziel_eins,testziel_zwei", "", dry=False, now_minutes=NOON)
check("über /targeting, enabled bleibt false",
      r["status"] == "ok" and bot.patches[0][0] == "/api/profiles/24/targeting"
      and bot.patches[0][1] == {"sources": {"blogger-followers": {"value": "testziel_eins,testziel_zwei",
                                                                  "enabled": False}}}
      and r["enabled_after"] is False, (r, bot.patches))
r = bot_local.bot_config("24", "blogger-followers", "", "testziel_eins, testziel_zwei", dry=False, now_minutes=NOON)
check("zurück auf leer (expect mit Leerzeichen)", r["status"] == "ok"
      and r["after"] == {"blogger-followers": ""}, r)

class Guarded(FakeBot):
    """Mindestbestand der Bot-App: kürzen wird still ignoriert (HTTP 200)."""
    def patch(self, path, token, payload):
        self.patches.append((path, payload))
        return 200, {"ok": True}
bot = use(Guarded(make_profile(targets="a,b"), enabled=False))
r = bot_local.bot_config("24", "blogger-followers", "", "a,b", dry=False, now_minutes=NOON)
check("Mindestbestand: reverted_or_failed mit Hinweis", r["status"] == "reverted_or_failed"
      and "Mindestbestand" in r.get("hint", ""), r)

check("bot-config ist eingreifend", remote_commands.HANDLERS["bot-config"][1] is True)

print("Zeitfenster-Beginn")
check("03:50 + 15 trifft 04:00", bot_local.slot_starts_within("04:00-05:59 + 16:00-17:59", 230, 15))
check("04:10 (schon drin) beginnt nicht", not bot_local.slot_starts_within("04:00-05:59", 250, 15))
check("23:50 + 15 trifft 00:00", bot_local.slot_starts_within("00:00-01:59", 23 * 60 + 50, 15))
check("run manually startet nie von selbst", not bot_local.slot_starts_within("run manually", 0, 15))


# ── Phone ─────────────────────────────────────────────────────────────

class FakePhone:
    def __init__(self, manufacturer="samsung", disabled=(), ota="null", installed=phone_control.UPDATE_PACKAGES,
                 state="device", focus="mResumedActivity: com.sec.android.app.launcher/.Launcher"):
        self.manufacturer, self.disabled, self.ota = manufacturer, set(disabled), ota
        self.installed, self.state, self.focus = set(installed), state, focus
        self.commands, self.rebooted, self.boot_polls = [], False, 0

    def shell(self, adb, serial, command, timeout=60):
        self.commands.append(command)
        if command.startswith("pm list packages -d "):
            p = command.split()[-1]
            return f"package:{p}" if p in self.disabled else ""
        if command.startswith("pm list packages "):
            p = command.split()[-1]
            return f"package:{p}" if p in self.installed else ""
        if command.startswith("pm disable-user"):
            self.disabled.add(command.split()[-1]); return "Package new state: disabled-user"
        if command.startswith("pm enable"):
            self.disabled.discard(command.split()[-1]); return "Package new state: enabled"
        if command.startswith("settings get"):
            return self.ota
        if command.startswith("settings put"):
            self.ota = command.split()[-1]; return ""
        if command == "getprop ro.product.manufacturer":
            return self.manufacturer
        if command == "getprop sys.boot_completed":
            self.boot_polls += 1
            return "1" if self.boot_polls >= 3 else ""
        if command == "cat /proc/uptime":
            return "123.45 456.78"
        if command.startswith("df"):
            return ("Filesystem Size Used Avail Use% Mounted on\n/dev/block/dm-6 21G 17G 4.4G 80% /data\n"
                    "/dev/block/dm-50 21G 17G 4.4G 80% /storage/emulated/0/Android/obb")
        if command.startswith("dumpsys activity"):
            return self.focus + "\nFailed to write while dumping service activity: Broken pipe"
        return "x"


def use_phone(phone):
    phone_control._shell = phone.shell
    phone_control._adb = lambda: "/fake/adb"
    phone_control.adb_state = lambda adb, serial: phone.state
    return phone


print("phone-info")
ph = use_phone(FakePhone(focus="mResumedActivity: com.instagram.androie/.MainActivity"))
info = phone_control.phone_info("R58WB02WFEF")
check("liest alles", info["online"] and info["uptime_seconds"] == 123 and info["df_data"].split()[-1] == "/data"
      and info["foreground"] == "mResumedActivity: com.instagram.androie/.MainActivity"
      and info["update_packages"]["com.wssyncmldm"] == {"installed": True, "enabled": True}
      and info["instagram_vorne"] and info["ota_disable_automatic_update"] == "null", info)
check("phone-info schreibt nichts", not any(c.startswith(("pm disable", "pm enable", "settings put", "reboot"))
                                            for c in ph.commands), ph.commands)
use_phone(FakePhone(state="offline"))
check("offline -> nur Zustand", phone_control.phone_info("R58") == {"serial": "R58", "adb": "offline", "online": False})
for bad in ("", "a,b", "a b", "R58;reboot"):
    try:
        phone_control.phone_info(bad)
        check(f"serial {bad!r} abgelehnt", False)
    except RolloutError:
        check(f"serial {bad!r} abgelehnt", True)
check("phone-info ist lesend", remote_commands.HANDLERS["phone-info"][1] is False)

print("samsung-updates")
ph = use_phone(FakePhone())
r = phone_control.samsung_updates("R58", "status")
check("status: nur lesen", r["status"] == "status" and not any(c.startswith(("pm disable", "settings put"))
                                                               for c in ph.commands), r)
r = remote_commands._cmd_samsung_updates({"serial": "R58", "action": "off"})
check("off ohne dry=false: nur Plan", r["status"] == "dry" and len(r["plan"]) == 3
      and not any(c.startswith(("pm disable", "settings put")) for c in ph.commands), r)
r = phone_control.samsung_updates("R58", "off", dry=False)
check("off: beide deaktiviert + Setting 1 -> ok", r["status"] == "ok" and ph.ota == "1"
      and ph.disabled == set(phone_control.UPDATE_PACKAGES), r)
r = phone_control.samsung_updates("R58", "off", dry=False)
check("off nochmal -> unchanged", r["status"] == "unchanged", r)
r = phone_control.samsung_updates("R58", "on", dry=False)
check("on: Rückweg -> ok", r["status"] == "ok" and ph.ota == "0" and not ph.disabled, r)
ph = use_phone(FakePhone(manufacturer="Xiaomi"))
r = phone_control.samsung_updates("R58", "off", dry=False)
check("kein Samsung -> refused, nichts geändert", r["status"] == "refused"
      and not any(c.startswith(("pm disable", "settings put")) for c in ph.commands), r)
ph = use_phone(FakePhone(installed=("com.wssyncmldm",)))
r = phone_control.samsung_updates("R58", "off", dry=False)
check("Paket fehlt -> refused", r["status"] == "refused" and "soagent" in r["reason"], r)
class Locked(Cfg):
    remote_allow_actions = False
remote_commands.get_config = lambda: Locked
try:
    remote_commands._cmd_samsung_updates({"serial": "R58", "action": "off"})
    check("off gesperrt ohne remote_allow_actions", False)
except remote_commands.CommandError:
    check("off gesperrt ohne remote_allow_actions", True)
use_phone(FakePhone())
check("status geht auch ohne remote_allow_actions",
      remote_commands._cmd_samsung_updates({"serial": "R58"})["status"] == "status")
remote_commands.get_config = lambda: Cfg

print("phone-reboot")
import device_monitor, ig_worker

reboots = []
device_monitor.restart_adb_device = lambda serial: reboots.append(serial) or True
phone_control._sleep = lambda s: None
clock = [0.0]
def tick():
    clock[0] += 5
    return clock[0]
phone_control._monotonic = tick

def accounts(*items):
    bot_local.device_profiles = lambda serial, token=None: [dict(i) for i in items]

accounts({"id": "24", "username": "kaumgesehen", "runState": "running", "timeSlot": "04:00-05:59 + 16:00-17:59"})
use_phone(FakePhone())
r = phone_control.phone_reboot("R58", dry=False, now_minutes=NOON)
check("Account läuft -> refused, kein reboot", r["status"] == "refused" and not reboots, r)

accounts({"id": "42", "username": "nichtimrahmen", "runState": "finished", "timeSlot": "00:00-01:59 + 12:00-13:59"})
r = phone_control.phone_reboot("R58", dry=False, now_minutes=11 * 60 + 50)
check("Zeitfenster in 10 min -> refused", r["status"] == "refused" and not reboots, r)

def broken(serial, token=None):
    raise bot_local.BotLocalError("HTTP 401")
bot_local.device_profiles = broken
r = phone_control.phone_reboot("R58", dry=False, now_minutes=NOON)
check("Bot-App nicht lesbar -> refused", r["status"] == "refused" and not reboots, r)

accounts({"id": "24", "username": "kaumgesehen", "runState": "finished", "timeSlot": "04:00-05:59 + 16:00-17:59"},
         {"id": "42", "username": "nichtimrahmen", "runState": "failed", "timeSlot": "00:00-01:59 + 12:00-13:59"})
r = remote_commands._cmd_phone_reboot({"serial": "R58"})
check("Standard ist dry", r["status"] in ("dry", "refused") and not reboots, r)
r = phone_control.phone_reboot("R58", dry=True, now_minutes=14 * 60)
check("dry: frei -> würde neu starten, kein reboot", r["status"] == "dry" and not reboots, r)

lock = ig_worker._phone_lock("R58")
lock.acquire()
r = phone_control.phone_reboot("R58", dry=False, now_minutes=14 * 60)
check("IG-Update hält das Phone -> refused", r["status"] == "refused" and not reboots, r)
lock.release()

ph = use_phone(FakePhone())
r = phone_control.phone_reboot("R58", dry=False, now_minutes=14 * 60)
check("frei: reboot, wartet auf boot_completed -> ok", r["status"] == "ok" and reboots == ["R58"]
      and ph.boot_polls == 3 and r["after"]["adb"] == "device" and not lock.locked(), r)

reboots.clear()
ph = use_phone(FakePhone()); ph.boot_polls = -10**6
clock[0] = 0.0
r = phone_control.phone_reboot("R58", dry=False, now_minutes=14 * 60)
check("bootet nicht in 180 s -> timeout", r["status"] == "timeout" and r["after"]["seconds"] >= 180, r)
check("phone-reboot ist eingreifend", remote_commands.HANDLERS["phone-reboot"][1] is True)

print()
if failures:
    print(f"{len(failures)} FEHLGESCHLAGEN")
    sys.exit(1)
print("Alle Prüfungen bestanden.")
