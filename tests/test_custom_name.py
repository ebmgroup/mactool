"""bot-custom-name: customName EINES Accounts über /v1/profiles/{id}/settings — dry, expect, Nachlesen.

Keine Bot-App: GET und PATCH werden durch eine Attrappe ersetzt, die mitschreibt, was gesendet würde.
"""
import copy, logging, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

import bot_local
import remote_commands

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


class Cfg:
    bot_admin_token = "ak_live_test"
    remote_allow_actions = True


bot_local.get_config = lambda: Cfg
remote_commands.get_config = lambda: Cfg

ALT = "IA reduziert bis 23.11. - A! UNFOLLOW FIRST - x@y.de | Light - c23/08/2025"


class FakeBot:
    def __init__(self, name=ALT, status=200, wirkt=True, platform="instagram", ohne_etag=False):
        self.etag = '"profile-24-abc"'
        self.ohne_etag = ohne_etag
        self.profile = {"id": "24", "username": "kaumgesehen", "platform": platform, "runState": "finished",
                        "customName": name}
        self.status, self.wirkt = status, wirkt
        self.patches = []

    def get(self, path, token=None, header=None):
        assert token == "ak_live_test", "Token fehlt"
        assert path == "/api/profiles/24", path
        return 200, {"ok": True, "profile": copy.deepcopy(self.profile)}

    def v1_patch(self, path, token, payload, idem, dry, if_match=None):
        self.patches.append({"path": path, "payload": payload, "idem": idem, "dry": dry, "if_match": if_match})
        if self.status != 200:
            return self.status, {"ok": False, "error": {"code": "legacy_key_not_allowed_on_v1"}}
        if not dry and if_match != self.etag:
            # wie die Bot-App: ohne If-Match 428, mit veraltetem 412
            return (428 if not if_match else 412), {"code": "missing_if_match" if not if_match else "precondition_failed"}
        if not dry and self.wirkt:
            self.profile["customName"] = payload["custom_name"]
        return 200, {"ok": True, "etag": self.etag if dry and not self.ohne_etag else None}


def use(bot):
    bot_local.get = bot.get
    bot_local._v1_patch = bot.v1_patch
    return bot


print("bot-custom-name: Eingaben")
for args in ({"profile": "all", "value": "x", "expect": ""}, {"profile": "24", "value": "  ", "expect": ""},
             {"profile": "24", "value": "x" * 201, "expect": ""}, {"profile": "../1", "value": "x", "expect": ""}):
    try:
        use(FakeBot())
        remote_commands._cmd_bot_custom_name(args)
        check(f"abgelehnt: {args['profile']!r}/{len(args['value'])} Zeichen", False)
    except remote_commands.CommandError:
        check(f"abgelehnt: {args['profile']!r}/{len(args['value'])} Zeichen", True)
try:
    remote_commands._cmd_bot_custom_name({"profile": "24", "value": "x"})
    check("ohne expect abgelehnt", False)
except remote_commands.CommandError:
    check("ohne expect abgelehnt", True)

print("bot-custom-name: Schutz")
bot = use(FakeBot())
r = bot_local.custom_name("24", "NEU | Pro", "etwas anderes", dry=False)
check("expect passt nicht -> refused, kein PATCH", r["status"] == "refused" and not bot.patches, r)
bot = use(FakeBot(platform="tiktok"))
r = bot_local.custom_name("24", "NEU | Pro", ALT, dry=False)
check("andere Plattform -> refused", r["status"] == "refused" and not bot.patches, r)
bot = use(FakeBot())
r = bot_local.custom_name("24", ALT, ALT, dry=False)
check("schon so -> unchanged, kein PATCH", r["status"] == "unchanged" and not bot.patches, r)
bot = use(FakeBot(name="A\x00B  c"))
r = bot_local.custom_name("24", "NEU", "AB  c", dry=False)
check("expect vergleicht bereinigt (NUL, Leerzeichen wie die Sync)", r["status"] == "ok", r)

print("bot-custom-name: Probelauf und Ausführen")
bot = use(FakeBot())
r = remote_commands._cmd_bot_custom_name({"profile": 24, "value": "A! UNFOLLOW FIRST\n- x@y.de | Light", "expect": ALT})
check("Standard ist dry: PATCH mit dry_run, Name unverändert",
      r["status"] == "dry" and len(bot.patches) == 1 and bot.patches[0]["dry"] is True
      and bot.profile["customName"] == ALT, (r, bot.patches))
check("Pfad + Body + einzeilig", bot.patches[0]["path"] == "/v1/profiles/24/settings"
      and bot.patches[0]["payload"] == {"custom_name": "A! UNFOLLOW FIRST - x@y.de | Light"}, bot.patches)
check("Idempotenz-Schlüssel gesetzt", len(bot.patches[0]["idem"]) == 40, bot.patches)

bot = use(FakeBot())
r = remote_commands._cmd_bot_custom_name({"profile": "24", "value": "NEU | Pro", "expect": ALT, "dry": "false"})
check("dry=false -> erst Probelauf, dann mit If-Match gespeichert, nachgelesen",
      r["status"] == "ok" and r["after"] == "NEU | Pro" and [x["dry"] for x in bot.patches] == [True, False]
      and bot.patches[1]["if_match"] == '"profile-24-abc"' and bot.patches[0]["idem"] != bot.patches[1]["idem"], (r, bot.patches))

bot = use(FakeBot(ohne_etag=True))
r = bot_local.custom_name("24", "NEU | Pro", ALT, dry=False)
check("Probelauf ohne ETag -> refused, kein echtes Speichern", r["status"] == "refused"
      and [x["dry"] for x in bot.patches] == [True], (r, bot.patches))

bot = use(FakeBot(wirkt=False))
r = bot_local.custom_name("24", "NEU | Pro", ALT, dry=False)
check("HTTP 200, aber Name nicht übernommen -> failed", r["status"] == "failed" and r["after"] == ALT, r)

bot = use(FakeBot(status=403))
r = bot_local.custom_name("24", "NEU | Pro", ALT, dry=True)
check("falscher Schlüssel -> dry_failed mit Hinweis", r["status"] == "dry_failed" and "ak_live" in r.get("hint", ""), r)

check("als eingreifend eingetragen", remote_commands.HANDLERS["bot-custom-name"][1] is True)

print()
print("FEHLGESCHLAGEN: " + ", ".join(failures) if failures else "Alle Prüfungen bestanden.")
sys.exit(1 if failures else 0)
