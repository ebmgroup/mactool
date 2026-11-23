"""Logging des Mactools: eine Datei pro Tag, nie mehr als LOG_DAYS Tage.

logs/mactool.log ist der laufende Tag, mactool.log.JJJJ-MM-TT die Vortage; was älter als
LOG_DAYS Tage ist, löscht der Handler beim Tageswechsel selbst.

Unter launchd (kein Terminal) zeigen stdout und stderr (fd 1/2) zusätzlich auf dieselbe
Datei. Sonst schriebe launchd sie nach logs/stdout.log und logs/stderr.log — ohne
Rotation, im November 2026 auf jedem Mac rund 600 MB. So landen auch Tracebacks aus
Threads, uvicorn und Ausgaben von Unterprozessen im Tageslog und verschwinden mit ihm.
Am Terminal bleibt alles wie gewohnt auf dem Bildschirm.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import date, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DAYS = 30
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LAUNCHD_FILES = ("stdout.log", "stderr.log")  # aus der plist in install.sh
_STARTUP_TAIL_BYTES = 4096
_OLD_NUMBERED = re.compile(r"^mactool\.log\.\d+$")  # Reste des früheren RotatingFileHandler


class DailyLogHandler(TimedRotatingFileHandler):
    """Rotiert um Mitternacht, behält heute + LOG_DAYS-1 Vortage; hängt fd 1/2 bei Bedarf mit um."""

    def __init__(self, path: Path, redirect_std: bool):
        self._redirect_std = redirect_std
        super().__init__(path, when="midnight", backupCount=LOG_DAYS - 1, encoding="utf-8")
        for old in self.getFilesToDelete():  # nach längerer Pause nicht erst um Mitternacht
            os.remove(old)
        self._attach_std()

    def getFilesToDelete(self) -> list[str]:
        """Zu viele Dateien ODER älter als LOG_DAYS Tage (Mac war aus: Lücken im Datum)."""
        doomed = set(super().getFilesToDelete())
        oldest_kept = (date.today() - timedelta(days=LOG_DAYS - 1)).isoformat()
        prefix = os.path.basename(self.baseFilename) + "."
        folder = os.path.dirname(self.baseFilename)
        for name in os.listdir(folder):
            day = name[len(prefix):]
            if name.startswith(prefix) and self.extMatch.fullmatch(day) and day < oldest_kept:
                doomed.add(os.path.join(folder, name))
        return sorted(doomed)

    def _attach_std(self) -> None:
        if not self._redirect_std or self.stream is None:
            return
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
        for fd in (1, 2):
            os.dup2(self.stream.fileno(), fd)

    def doRollover(self) -> None:
        super().doRollover()
        self._attach_std()


def _take_launchd_output(log_dir: Path) -> list[str]:
    """Ende der launchd-Dateien merken (Absturz vor dem Logging-Start) und sie leeren."""
    notes = []
    for name in LAUNCHD_FILES:
        path = log_dir / name
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size == 0:
            continue
        with open(path, "rb") as handle:
            handle.seek(max(0, size - _STARTUP_TAIL_BYTES))
            tail = handle.read().decode("utf-8", errors="replace")
        if size > _STARTUP_TAIL_BYTES:
            tail = tail.split("\n", 1)[-1]  # angeschnittene erste Zeile weg
        os.truncate(path, 0)
        notes.append(f"{name} ({size} Bytes) geleert, Ende davon:\n{tail.rstrip()}")
    return notes


def setup_logging(log_dir: Path, level: int = logging.INFO, interactive: bool | None = None) -> None:
    """Root-Logger einrichten. interactive=None: am Terminal ja, unter launchd nein."""
    if interactive is None:
        interactive = sys.stderr.isatty()
    log_dir.mkdir(exist_ok=True)

    for old in log_dir.iterdir():
        if _OLD_NUMBERED.match(old.name):
            old.unlink(missing_ok=True)

    notes = [] if interactive else _take_launchd_output(log_dir)

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = [DailyLogHandler(log_dir / "mactool.log", redirect_std=not interactive)]
    if interactive:
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.setLevel(level)
        root.addHandler(handler)

    for note in notes:
        logging.getLogger(__name__).info(note)
