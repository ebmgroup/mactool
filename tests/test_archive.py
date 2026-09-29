"""Archiv: Upload der letzten 7 Tage, Bucket-Aufräumen, LOKALES Aufräumen nach 60 Tagen.

Das lokale Löschen ist der heikle Teil (Andreas 29.09.2026: „es darf nie etwas
neueres als 60 Tage gelöscht werden") — darum wird jede Schutzregel einzeln geprüft.
Kein Netz, kein Mac: Storage ist ein Nachbau im Speicher.
"""
import io, json, logging, os, sys, tempfile, time, zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

import archive_uploader as au

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


def zipdatei(pfad: Path, when: datetime, reason=None, mtime: datetime | None = None, png=b"\x89PNG fake"):
    with zipfile.ZipFile(pfad, "w") as z:
        z.writestr("reason.json", json.dumps(reason or {"type": "crash", "account": "konto", "cause": {"message": "x"}}))
        z.writestr("hierarchy.xml", '<hierarchy><node text="Try again later" content-desc="" resource-id="a"/></hierarchy>')
        z.writestr("logs.txt", "a\nb\nletzte")
        z.writestr("screenshot.png", png)
    t = (mtime or when).timestamp()
    os.utime(pfad, (t, t))


def name(when: datetime) -> str:
    return f"3.7.9b0_{when:%Y-%m-%d-%H-%M-%S}.zip"


class FakeBucket:
    def __init__(self, store):
        self.store = store

    def upload(self, path, data, file_options=None):
        self.store[path] = data

    def list(self, prefix, options=None):
        tiefe = prefix.count("/") + 1
        namen = set()
        for k in self.store:
            if k.startswith(prefix + "/"):
                namen.add(k.split("/")[tiefe])
        return [{"name": n} for n in sorted(namen)]

    def remove(self, pfade):
        for p in pfade:
            self.store.pop(p, None)


class FakeStorage:
    def __init__(self):
        self.store = {}

    def get_bucket(self, name):
        return {"name": name}

    def from_(self, name):
        return FakeBucket(self.store)


class FakeClient:
    def __init__(self):
        self.storage = FakeStorage()


jetzt = datetime(2026, 9, 29, 12, 0, 0)
utc_jetzt = jetzt.astimezone(timezone.utc)

with tempfile.TemporaryDirectory() as tmp:
    au._STATE_FILE = Path(tmp) / "state.json"
    storage = Path(tmp) / "GramBotStorage"
    w, c = storage / "warnings", storage / "crashes"
    w.mkdir(parents=True)
    c.mkdir()

    # ── lokales Aufräumen ────────────────────────────────────────────
    alt = jetzt - timedelta(days=61)
    grenzfall = jetzt - timedelta(days=59, hours=23)
    zipdatei(w / name(alt), alt)                                     # darf weg
    zipdatei(c / name(alt - timedelta(days=100)), alt - timedelta(days=100))  # darf weg
    zipdatei(w / name(grenzfall), grenzfall)                         # 59 Tage: bleibt
    zipdatei(w / name(jetzt - timedelta(days=1)), jetzt - timedelta(days=1))  # neu: bleibt
    alt_name_neu_mtime = jetzt - timedelta(days=90)
    zipdatei(w / name(alt_name_neu_mtime), alt_name_neu_mtime, mtime=jetzt - timedelta(days=2))  # Name alt, Datei neu: bleibt
    neu_name_alt_mtime = jetzt - timedelta(days=3)
    zipdatei(c / name(neu_name_alt_mtime), neu_name_alt_mtime, mtime=jetzt - timedelta(days=200))  # Name neu: bleibt
    fremd = w / "wichtig_2020-01-01.zip"
    zipdatei(fremd, alt)                                             # fremdes Muster: bleibt
    txt = w / "3.7.9b0_2020-01-01-00-00-00.txt"
    txt.write_text("x"); os.utime(txt, (alt.timestamp(), alt.timestamp()))  # keine .zip: bleibt
    tief = w / "unter"; tief.mkdir()
    zipdatei(tief / name(alt), alt)                                  # Unterordner: bleibt
    anderer = storage / "logs"; anderer.mkdir()
    zipdatei(anderer / name(alt), alt)                               # anderer Ordner: bleibt
    link = c / name(alt - timedelta(days=5))
    link.symlink_to(w / name(alt))                                   # Symlink: bleibt

    liste = sorted(p.name for p in au.lokal_zu_loeschen(storage, jetzt))
    erwartet = sorted([name(alt), name(alt - timedelta(days=100))])
    check("löscht genau die zwei ZIPs älter als 60 Tage", liste == erwartet, liste)

    r = au.cleanup_local(storage, None, now_local=jetzt)
    check("ohne Serverzeit wird nichts gelöscht", r["status"] == "skipped_no_server_time" and (w / name(alt)).exists(), r)
    r = au.cleanup_local(storage, utc_jetzt + timedelta(days=3), now_local=jetzt)
    check("bei falscher Mac-Uhr wird nichts gelöscht", r["status"] == "skipped_clock_skew" and (w / name(alt)).exists(), r)
    r = au.cleanup_local(storage, utc_jetzt, ausfuehren=False, now_local=jetzt)
    check("Vorschau löscht nichts", r["status"] == "dry_run" and r["kandidaten"] == 2 and (w / name(alt)).exists(), r)
    r = au.cleanup_local(storage, utc_jetzt + timedelta(minutes=45), now_local=jetzt)
    check("löscht mit 45 min Uhrabweichung", r["geloescht"] == 2, r)
    bleibt = [w / name(grenzfall), w / name(jetzt - timedelta(days=1)), w / name(alt_name_neu_mtime),
              c / name(neu_name_alt_mtime), fremd, txt, tief / name(alt), anderer / name(alt)]
    check("alles Jüngere und Fremde ist noch da", all(p.exists() for p in bleibt), [str(p) for p in bleibt if not p.exists()])
    check("Symlink nicht angefasst", link.is_symlink())
    check("Frist ist 60 Tage", au.LOCAL_RETENTION_DAYS == 60)

    # ── Upload ────────────────────────────────────────────────────────
    au._jpeg = lambda png: (b"JPEG", "image/jpeg")  # sips gibt es hier evtl. nicht
    client = FakeClient()
    r = au.upload_archive(client, "mac05", storage, now=jetzt)
    oben = client.storage.store
    # im Fenster (7 Tage): 1 Tag alt (warning), 3 Tage alt (crash); 2-Tage-mtime-Datei hat Namensdatum 90 Tage → nicht
    check("lädt nur ZIPs der letzten 7 Tage", r["uploaded"] == 2, (r, sorted(oben)))
    js = [k for k in oben if k.endswith(".json")]
    meta = json.loads(oben[sorted(js)[0]])
    check("Pfad mac/datum/art-name", all(k.startswith("mac05/2026-09-") and ("/warning-" in k or "/crash-" in k) for k in oben), sorted(oben))
    check("je ZIP Bild, JSON und Log", len(oben) == 6, sorted(oben))
    check("Metadaten mit reason, Bildschirm, Log, Bild", meta.get("reason", {}).get("account") == "konto"
          and meta["screen"][0]["text"] == "Try again later" and meta["log_tail"][-1] == "letzte" and meta["bild"].endswith(".jpg"), meta)
    check("Log-Ende als eigene Datei", meta.get("log", "").endswith(".log") and oben[meta["log"]] == b"a\nb\nletzte"
          and "_log" not in meta, meta)
    r2 = au.upload_archive(client, "mac05", storage, now=jetzt)
    check("zweiter Lauf lädt nichts doppelt", r2["uploaded"] == 0, r2)

    # ── Bucket aufräumen ──────────────────────────────────────────────
    oben["mac05/2026-09-10/crash-alt.json"] = b"{}"
    oben["mac05/2026-09-10/crash-alt.jpg"] = b"x"
    oben["mac06/2026-09-10/crash-fremd.json"] = b"{}"
    n = au.cleanup_bucket(client, "mac05", jetzt)
    check("Bucket: alte Tage dieses Macs weg", n == 2 and not any(k.startswith("mac05/2026-09-10") for k in oben), sorted(oben))
    check("Bucket: anderer Mac und neue Tage bleiben", "mac06/2026-09-10/crash-fremd.json" in oben and len([k for k in oben if k.startswith("mac05/")]) == 6, sorted(oben))

    # Neuer Stand → alles im Fenster noch einmal
    st = au._load_state(); st["stand"] = 1; au._save_state(st)
    r3 = au.upload_archive(client, "mac05", storage, now=jetzt)
    check("neuer Stand lädt das Fenster neu", r3["uploaded"] == 2, r3)

print()
if failures:
    print(f"{len(failures)} FEHLER: {failures}")
    sys.exit(1)
print("Alle Archiv-Tests bestanden.")
