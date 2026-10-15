"""API routes, key masking, config round-trip and the dashboard stats toggle."""
import json, logging, sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

from fastapi.testclient import TestClient
import config as cfg_mod
import api
import sync

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


client = TestClient(api.app)

# ── 1. Routes present ────────────────────────────────────────────────
print("\n[1] Routen")
routes = {r.path for r in api.app.routes}
for path in [
    "/api/stats/settings", "/api/stats/dashboard/run", "/api/stats/customer/run",
    "/api/stats/customer/preview", "/api/stats/test",
    "/api/legacy-upload", "/api/legacy-upload/disable", "/api/legacy-upload/enable",
]:
    check(f"{path} registriert", path in routes)

# ── 2. Key masking ───────────────────────────────────────────────────
print("\n[2] Key-Maskierung")
cfg = cfg_mod.get_config()
cfg.customer_stats_key = "supersecret-key-ABCD"
cfg.supabase_key = "dashboard-key-WXYZ"

settings = client.get("/api/stats/settings").json()
masked = settings["customer"]["stats"]["key_masked"]
check("Key maskiert", masked == "••••••••ABCD", masked)
check("Klartext-Key nicht im JSON", "supersecret" not in json.dumps(settings))
check("URL bleibt sichtbar", settings["customer"]["stats"]["url"] == cfg.customer_stats_url)
check("dashboard-Key maskiert", settings["dashboard"]["key_masked"] == "••••••••WXYZ")
check("Schema wird mitgeliefert", settings["customer"]["users"]["schema"] == cfg.customer_users_schema)

# ── 3. Config round-trip ─────────────────────────────────────────────
print("\n[3] Config speichern")
resp = client.put("/api/config", json={
    "customer_stats_enabled": True,
    "customer_stats_source": "superdb",
    "customer_stats_table": "statistik2",
    "customer_stats_session_limit": 30,
    "dashboard_stats_enabled": False,
    "customer_users_schema": "public",
})
check("PUT ok", resp.status_code == 200, resp.text[:200])
check("Key unveraendert ohne Eingabe", cfg.customer_stats_key == "supersecret-key-ABCD",
      cfg.customer_stats_key)
check("Toggle uebernommen", cfg.customer_stats_enabled is True)
check("Quelle uebernommen", cfg.customer_stats_source == "superdb")
check("Tabelle uebernommen", cfg.customer_stats_table == "statistik2")
check("Limit uebernommen", cfg.customer_stats_session_limit == 30)
check("dashboard_stats_enabled aus", cfg.dashboard_stats_enabled is False)

client.put("/api/config", json={"customer_stats_key": ""})
check("leerer Key laesst Wert stehen", cfg.customer_stats_key == "supersecret-key-ABCD")

client.put("/api/config", json={"customer_stats_key": "••••••••ABCD"})
check("Maske zurueckgeschickt aendert nichts", cfg.customer_stats_key == "supersecret-key-ABCD")

client.put("/api/config", json={"customer_stats_key": "neuer-key"})
check("neuer Key wird gesetzt", cfg.customer_stats_key == "neuer-key", cfg.customer_stats_key)

client.put("/api/config", json={"customer_stats_key": "-"})
check("'-' loescht den Key", cfg.customer_stats_key == "")

bad = client.put("/api/config", json={"customer_stats_source": "quatsch"})
check("ungueltige Quelle wird abgelehnt", bad.status_code == 400, str(bad.status_code))
check("Quelle unveraendert nach Fehler", cfg.customer_stats_source == "superdb")

client.put("/api/config", json={"customer_stats_table": "   "})
check("leerer Tabellenname wird ignoriert", cfg.customer_stats_table == "statistik2",
      cfg.customer_stats_table)
client.put("/api/config", json={"customer_users_schema": "   "})
check("leeres Schema wird ignoriert", cfg.customer_users_schema == "public", cfg.customer_users_schema)

# ── 4. Status endpoint ───────────────────────────────────────────────
print("\n[4] /api/status")
status = client.get("/api/status").json()
for field in ("dashboard_stats_enabled", "customer_stats_enabled", "customer_stats_source", "last_runs"):
    check(f"status enthaelt {field}", field in status)

# ── 5. Dashboard stats toggle in sync ────────────────────────────────
print("\n[5] stats-Toggle im Sync")
cfg.dashboard_stats_enabled = False
cfg.customer_stats_table = "statistik"
cfg.sqlite_db_path = "GramBotStorage/super.db"
cfg.supabase_url = "http://127.0.0.1:9"   # unreachable on purpose
cfg.supabase_key = "x"

result = sync.trigger_sync()
tables = result.get("tables", {})
check("stats wurde uebersprungen", tables.get("stats", {}).get("status") == "disabled",
      str(tables.get("stats")))
check("device wurde trotzdem versucht", tables.get("device", {}).get("status") != "disabled",
      str(tables.get("device", {}).get("status")))
check("profile wurde trotzdem versucht", tables.get("profile", {}).get("status") != "disabled",
      str(tables.get("profile", {}).get("status")))

cfg.dashboard_stats_enabled = True
result = sync.trigger_sync()
check("mit Toggle an ist stats nicht disabled",
      result["tables"].get("stats", {}).get("status") != "disabled",
      str(result["tables"].get("stats")))

check("Tabellennamen kommen aus der Config",
      sync.get_table_mappings() == {"device": "device", "profile": "profile", "stats": "stats"},
      str(sync.get_table_mappings()))
cfg.dashboard_table_stats = "stats_custom"
check("geaenderter Tabellenname greift", sync.get_table_mappings()["stats"] == "stats_custom")
cfg.dashboard_table_stats = "stats"

# ── 6. Unknown config keys are ignored ───────────────────────────────
print("\n[6] Downgrade-Sicherheit")
Path("config_downgrade.json").write_text(json.dumps({
    "server_name": "mac17",
    "supabase_key": "abc",
    "eine_zukuenftige_option": True,
}))
old_file = cfg_mod.CONFIG_FILE
cfg_mod.CONFIG_FILE = Path("config_downgrade.json")
try:
    loaded = cfg_mod.AppConfig.load()
    check("unbekannter Key crasht nicht", loaded.server_name == "mac17", loaded.server_name)
    check("bekannte Werte kommen an", loaded.supabase_key == "abc")
finally:
    cfg_mod.CONFIG_FILE = old_file
    Path("config_downgrade.json").unlink()

print("\n[Bot-App] Admin-Token wie ein Key behandelt")
cfg.bot_admin_token = "i50rgeheimes-token-WXYZ"
s = client.get("/api/stats/settings").json()
check("Token maskiert", s["bot"]["admin_token_masked"] == "••••••••WXYZ", s.get("bot"))
check("Token nie im Klartext", "geheimes-token" not in json.dumps(s))
client.put("/api/config", json={"bot_admin_token": ""})
check("leer laesst Token stehen", cfg.bot_admin_token == "i50rgeheimes-token-WXYZ")
client.put("/api/config", json={"bot_admin_token": "••••••••WXYZ"})
check("Maske zurueck aendert nichts", cfg.bot_admin_token == "i50rgeheimes-token-WXYZ")
client.put("/api/config", json={"bot_admin_token": "neues-token"})
check("neues Token gesetzt", cfg.bot_admin_token == "neues-token")
client.put("/api/config", json={"bot_admin_token": "-"})
check("'-' loescht Token", cfg.bot_admin_token == "")

import bot_local, types
calls = []
class R:
    def __init__(self, status, body): self.status_code, self._b = status, body
    def json(self): return self._b
def fake_get(url, headers=None, timeout=None):
    calls.append((url, dict(headers or {})))
    if url.endswith("/ui/runtime/instagram"): return R(200, {"ok": True})
    if (headers or {}).get("X-Admin-Key") == "richtig": return R(200, {"devices": [1, 2, 3]})
    return R(401, {"error": "Missing X-API-Key header"})
bot_local.requests = types.SimpleNamespace(get=fake_get, RequestException=Exception)
r = client.post("/api/bot/local-test").json()
check("ohne Token: erreichbar, aber Hinweis", r["ok"] is False and r.get("reachable") is True, r)
cfg.bot_admin_token = "richtig"
r = client.post("/api/bot/local-test").json()
check("Token mit X-Admin-Key erkannt", r["ok"] and r["header"] == "X-Admin-Key" and r["devices"] == 3, r)
check("nur lesende Abfragen an die Bot-App", all("/api/devices" in u or "/ui/runtime" in u for u, _ in calls), calls)
cfg.bot_admin_token = "falsch"
r = client.post("/api/bot/local-test").json()
check("falsches Token klar gemeldet", r["ok"] is False and "nicht angenommen" in r["message"], r)
cfg.bot_admin_token = ""

print("\n" + ("ALLE TESTS BESTANDEN" if not failures else f"{len(failures)} FEHLGESCHLAGEN: {failures}"))
sys.exit(1 if failures else 0)
