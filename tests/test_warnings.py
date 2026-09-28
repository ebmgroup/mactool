"""Warn-Archive: Suche, sichere Namensauflösung, Bildschirmtexte, Stückweises Holen.

Baut ein kleines Warn-ZIP wie GramAddict es ablegt (png, xml, logs.txt) in einem
Temp-Ordner. Kein Netz, kein Mac.
"""
import base64, logging, os, sys, tempfile, zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
logging.basicConfig(level=logging.CRITICAL)

import warnings_access as wa

failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


XML = b"""<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" resource-id="com.instagram.android:id/dialog_container" text="" content-desc="">
    <node class="android.widget.TextView" resource-id="com.instagram.android:id/igds_headline_headline" text="Try Again Later" content-desc="" bounds="[0,0][10,10]"/>
    <node class="android.widget.TextView" resource-id="com.instagram.android:id/igds_headline_body" text="We restrict certain activity to protect our community." content-desc=""/>
    <node class="android.widget.Button" resource-id="" text="" content-desc="OK"/>
    <node class="android.view.View" resource-id="" text="" content-desc=""/>
  </node>
</hierarchy>"""

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    storage = home / "Desktop" / "GramBotStorage"
    (storage / "logs").mkdir(parents=True)
    wdir = storage / "warnings"
    wdir.mkdir()
    (home / "Library" / "warnings").mkdir(parents=True)  # uebersprungen
    png = os.urandom(300_000)
    name = "3.7.9b0_2026-09-27-04-50-49.zip"
    with zipfile.ZipFile(wdir / name, "w") as z:
        z.writestr("screenshot.png", png)
        z.writestr("dump.xml", XML)
        z.writestr("logs.txt", "\n".join(f"Zeile {i}" for i in range(100)))
    (wdir / "notiz.txt").write_text("x")

    dirs = wa.find_warning_dirs([storage, storage.parent, home])
    check("findet den warnings-Ordner einmal", dirs == [wdir.resolve()], dirs)

    lst = wa.list_warnings(dirs)
    check("listet nur ZIPs", [f["name"] for f in lst["files"]] == [name], lst)
    check("grep filtert", wa.list_warnings(dirs, "2025")["total"] == 0)

    for bad in ["../../etc/passwd", "notiz.txt", "fehlt.zip", None]:
        try:
            wa.resolve_warning(dirs, bad)
            check(f"lehnt {bad!r} ab", False)
        except wa.WarningsError:
            check(f"lehnt {bad!r} ab", True)
    p = wa.resolve_warning(dirs, "../" + name)
    check("reduziert Pfade auf den Namen", p == (wdir / name).resolve(), p)

    t = wa.warning_text(p)
    texte = [(x["text"], x["desc"]) for x in t["screen"]]
    check("liest die Bildschirmtexte", ("Try Again Later", "") in texte and ("", "OK") in texte, texte)
    check("ohne leere Knoten", len(texte) == 3, texte)
    check("Log-Ende dabei", t["log_tail"][-1] == "Zeile 99", t["log_tail"][-3:])

    teile, off = [], 0
    while off is not None:
        r = wa.warning_part(p, "screenshot.png", off)
        check(f"Stueck bei {off} passt in die Queue", len(r["data_b64"]) <= wa.MAX_PART_CHARS, len(r["data_b64"]))
        teile.append(base64.b64decode(r["data_b64"]))
        off = r["next_offset"]
    check("Stuecke ergeben die Datei", b"".join(teile) == png, len(b"".join(teile)))
    try:
        wa.warning_part(p, "../x", 0)
        check("unbekanntes Mitglied abgelehnt", False)
    except wa.WarningsError:
        check("unbekanntes Mitglied abgelehnt", True)

print()
if failures:
    print(f"{len(failures)} FEHLER: {failures}")
    sys.exit(1)
print("Alle Warnings-Tests bestanden.")
