"""Logging: Tagesdateien, nie älter als 30 Tage, stdout/stderr unter launchd ins Tageslog.

Jeder Fall läuft in einem eigenen Python-Prozess: setup_logging biegt fd 1/2 um und
richtet den Root-Logger ein — das soll den Testprozess selbst nicht treffen.
"""
import os, subprocess, sys, tempfile, textwrap
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
failures = []


def check(name, condition, detail=""):
    print(("  PASS  " if condition else "  FAIL  ") + name + ("" if condition else f" :: {detail}"))
    if not condition:
        failures.append(name)


def run(logs: Path, body: str, interactive: bool) -> subprocess.CompletedProcess:
    code = textwrap.dedent(f"""
        import sys, logging
        sys.path.insert(0, {str(REPO)!r})
        from pathlib import Path
        import log_setup
        log_setup.setup_logging(Path({str(logs)!r}), interactive={interactive})
    """) + textwrap.dedent(body)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)


def day(n):
    return (date.today() - timedelta(days=n)).isoformat()


# ── 1. launchd: stderr.log/stdout.log geleert, Ende ins Log, fd 1/2 im Tageslog ──
logs = Path(tempfile.mkdtemp(prefix="logs-"))
(logs / "stderr.log").write_text("x" * 10000 + "\nTraceback: ImportError vor dem Start\n")
(logs / "stdout.log").write_text("")
for n in (1, 2, 3):
    (logs / f"mactool.log.{n}").write_text("alt")
for n in range(1, 45):  # Tage 1..44 zurück, Lücke 10..14 (Mac aus)
    if not 10 <= n <= 14:
        (logs / f"mactool.log.{day(n)}").write_text("tag")
(logs / "mactool.log.notiz").write_text("fremd")

res = run(logs, """
    logging.getLogger("t").info("ueber logging")
    print("ueber print", flush=True)
    sys.stderr.write("ueber stderr\\n"); sys.stderr.flush()
    import subprocess; subprocess.run(["echo", "ueber unterprozess"])
    import os; os.write(2, b"ueber fd2\\n")
""", interactive=False)
check("Prozess läuft durch", res.returncode == 0, res.stderr)
check("nichts mehr am echten stdout/stderr", res.stdout == "" and res.stderr == "", (res.stdout, res.stderr))
text = (logs / "mactool.log").read_text()
for what in ("ueber logging", "ueber print", "ueber stderr", "ueber unterprozess", "ueber fd2"):
    check(f"im Tageslog: {what}", what in text, text[-500:])
check("stderr.log geleert", (logs / "stderr.log").stat().st_size == 0)
check("Ende von stderr.log ins Log übernommen", "ImportError vor dem Start" in text)
check("angeschnittene Zeile verworfen", "x" * 100 not in text)
check("alte nummerierte Dateien weg", not any(logs.glob("mactool.log.[0-9]")))
kept = sorted(p.name[len("mactool.log."):] for p in logs.glob("mactool.log.????-??-??"))
check("nichts älter als 30 Tage", kept and min(kept) >= day(29), kept[:3])
check("Tage innerhalb der Frist bleiben", day(1) in kept and day(29) in kept and len(kept) == 24, kept)
check("fremde Dateien bleiben", (logs / "mactool.log.notiz").exists())

# ── 2. Tageswechsel: neue Datei, fd 1/2 ziehen mit ──
logs = Path(tempfile.mkdtemp(prefix="logs-"))
res = run(logs, """
    import os
    h = [h for h in logging.getLogger().handlers if isinstance(h, log_setup.DailyLogHandler)][0]
    print("vorher", flush=True)
    h.doRollover()
    print("nachher", flush=True)
    os.write(2, b"fd2 nachher\\n")
""", interactive=False)
rotated = list(logs.glob("mactool.log.????-??-??"))
check("Rollover legt Tagesdatei an", len(rotated) == 1, list(logs.iterdir()))
if rotated:
    check("alte Ausgabe in der Tagesdatei", "vorher" in rotated[0].read_text())
    now = (logs / "mactool.log").read_text()
    check("neue Ausgabe in der neuen Datei", "nachher" in now and "fd2 nachher" in now and "vorher" not in now, now)

# ── 3. Terminal: nichts umgebogen, nichts geleert ──
logs = Path(tempfile.mkdtemp(prefix="logs-"))
(logs / "stderr.log").write_text("bleibt\n")
res = run(logs, """
    logging.getLogger("t").info("am terminal")
    print("print am terminal")
""", interactive=True)
check("Terminal: Log auf dem Bildschirm", "am terminal" in res.stderr, res.stderr)
check("Terminal: print auf dem Bildschirm", "print am terminal" in res.stdout)
check("Terminal: auch in der Datei", "am terminal" in (logs / "mactool.log").read_text())
check("Terminal: stderr.log unangetastet", (logs / "stderr.log").read_text() == "bleibt\n")

print(f"\n{len(failures)} Fehler" if failures else "\nAlle Prüfungen bestanden.")
sys.exit(1 if failures else 0)
