"""Warn-Archive des Bots lesen — rein lesend.

GramAddict legt bei auffälligen Situationen ein ZIP in einen Ordner `warnings/`
(„Warning saved as warnings/3.7.9b0_2026-09-27-20-58-23.zip"): Screenshot (.png),
UI-Abbild (.xml), Log-Auszug, manchmal ein Video. Nur dort steht, welches Fenster
Instagram tatsächlich gezeigt hat — der Bot schreibt den Text nie ins Log.

Der Ordner liegt relativ zum Arbeitsverzeichnis des Bots — dort, wo auch seine
`logs/` liegen (`GramBotStorage`). Gesucht wird nur flach darin; ausserhalb werden
wenige feste Pfade geprueft, nie durchsucht (siehe SEARCH_DEPTH).

Nichts hier schreibt oder löscht etwas. Dateinamen werden auf ihren Namensanteil
reduziert und nur innerhalb der gefundenen `warnings`-Ordner aufgelöst.
"""

from __future__ import annotations

import base64
import os
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

# Nur innerhalb des Speicherordners wird gesucht — der Bot schreibt `logs/` und
# `warnings/` relativ zu seinem Arbeitsverzeichnis, und seine Logs liegen in
# `GramBotStorage/logs`. v1.0.123 durchsuchte auch das Home-Verzeichnis: dabei
# fasst macOS geschuetzte Ordner (Dokumente, Downloads …) an, fragt auf dem
# Bildschirm nach einer Berechtigung und haelt den Aufruf an, bis jemand
# antwortet — der Fernzugriff auf mac05 stand still. Darum ausserhalb nur feste
# Pfade, nie eine Suche.
SEARCH_DEPTH = 2
SKIP_DIRS = {"logs", "node_modules", ".git"}

MAX_LISTED = 200
MAX_TEXT_NODES = 300
# Ergebnisse laufen als jsonb durch die Queue, dort ist bei 200.000 Zeichen Schluss.
MAX_PART_CHARS = 150_000


class WarningsError(RuntimeError):
    """Datei oder Teil nicht gefunden oder nicht erlaubt."""


def find_warning_dirs(bases: list[Path], depth: int = SEARCH_DEPTH, fixed: list[Path] | None = None) -> list[Path]:
    """`warnings`-Ordner: feste Kandidaten plus eine flache Suche unter `bases`.

    `bases` duerfen nur Ordner sein, auf die das Tool ohnehin zugreift (der
    Speicherordner) — siehe Kommentar zu SEARCH_DEPTH.
    """
    found: list[Path] = []
    seen: set[Path] = set()
    for f in fixed or []:
        f = f.expanduser()
        try:
            if f.name == "warnings" and f.is_dir() and f.resolve() not in seen:
                seen.add(f.resolve())
                found.append(f.resolve())
        except OSError:
            continue
    for base in bases:
        base = base.expanduser()
        if not base.is_dir():
            continue
        base_depth = len(base.parts)
        for root, dirs, _files in os.walk(base):
            here = Path(root)
            if len(here.parts) - base_depth >= depth:
                dirs[:] = []
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if here.name == "warnings":
                resolved = here.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
                dirs[:] = []
    return found


def default_bases(sqlite_db_path: str) -> list[Path]:
    """Nur der Speicherordner wird durchsucht."""
    return [Path(sqlite_db_path).expanduser().parent]


def default_fixed(sqlite_db_path: str, bot_app_path: str | None = None) -> list[Path]:
    """Feste Kandidaten ausserhalb — nur geprueft, nie durchsucht."""
    storage = Path(sqlite_db_path).expanduser().parent
    out = [storage.parent / "warnings", Path.home() / "warnings"]
    if bot_app_path:
        out.append(Path(bot_app_path).expanduser().parent / "warnings")
    return out


def list_warnings(dirs: list[Path], grep: str | None = None) -> dict:
    """ZIP-Dateien der Warn-Ordner, neueste zuerst."""
    entries = []
    for d in dirs:
        for p in d.glob("*.zip"):
            try:
                st = p.stat()
            except OSError:
                continue
            if grep and grep not in p.name:
                continue
            entries.append(
                {
                    "name": p.name,
                    "dir": str(d),
                    "bytes": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                }
            )
    entries.sort(key=lambda e: e["modified"], reverse=True)
    return {
        "dirs": [str(d) for d in dirs],
        "total": len(entries),
        "listed": min(len(entries), MAX_LISTED),
        "files": entries[:MAX_LISTED],
    }


def resolve_warning(dirs: list[Path], name: str | None) -> Path:
    """Einen Dateinamen sicher auf ein ZIP in einem der Warn-Ordner abbilden."""
    clean = Path(name or "").name
    if not clean.endswith(".zip"):
        raise WarningsError("Es sind nur .zip-Dateien aus den warnings-Ordnern erlaubt")
    for d in dirs:
        candidate = (d / clean).resolve()
        if candidate.parent == d.resolve() and candidate.is_file():
            return candidate
    raise WarningsError(f"'{clean}' liegt in keinem warnings-Ordner")


def _members(z: zipfile.ZipFile) -> list[dict]:
    return [{"name": i.filename, "bytes": i.file_size} for i in z.infolist() if not i.is_dir()]


def screen_texts(xml: bytes) -> list[dict]:
    """Was auf dem Bildschirm stand: alle Knoten mit Text oder Beschreibung."""
    root = ElementTree.fromstring(xml)
    out = []
    for node in root.iter("node"):
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        if not text and not desc:
            continue
        out.append(
            {
                "text": text,
                "desc": desc,
                "id": node.get("resource-id") or "",
                "class": (node.get("class") or "").rsplit(".", 1)[-1],
                "bounds": node.get("bounds") or "",
            }
        )
        if len(out) >= MAX_TEXT_NODES:
            break
    return out


def warning_text(path: Path) -> dict:
    """Inhalt eines Warn-ZIPs und die Bildschirmtexte aus dem UI-Abbild."""
    with zipfile.ZipFile(path) as z:
        members = _members(z)
        xml_names = [m["name"] for m in members if m["name"].lower().endswith(".xml")]
        log_names = [m["name"] for m in members if m["name"].lower().endswith(".txt")]
        texts: list[dict] = []
        if xml_names:
            try:
                texts = screen_texts(z.read(xml_names[0]))
            except ElementTree.ParseError as e:
                texts = [{"error": f"XML nicht lesbar: {e}"}]
        log_tail: list[str] = []
        if log_names:
            lines = z.read(log_names[0]).decode("utf-8", "replace").splitlines()
            log_tail = [l[:500] for l in lines[-40:]]
    return {"name": path.name, "members": members, "xml": xml_names[:1], "screen": texts, "log_tail": log_tail}


def warning_part(path: Path, member: str, offset: int = 0) -> dict:
    """Ein Stück einer Datei aus dem ZIP, base64 — zum Zusammensetzen auf der Gegenseite."""
    if offset < 0:
        raise WarningsError("offset muss >= 0 sein")
    with zipfile.ZipFile(path) as z:
        names = {m["name"] for m in _members(z)}
        if member not in names:
            raise WarningsError(f"'{member}' ist nicht im Archiv (vorhanden: {', '.join(sorted(names))})")
        data = z.read(member)
    # base64 von n Bytes ergibt 4*ceil(n/3) Zeichen; 3er-Blöcke halten die Stücke einzeln dekodierbar.
    chunk_bytes = (MAX_PART_CHARS // 4) * 3
    piece = data[offset : offset + chunk_bytes]
    nxt = offset + len(piece)
    return {
        "member": member,
        "total_bytes": len(data),
        "offset": offset,
        "next_offset": nxt if nxt < len(data) else None,
        "data_b64": base64.b64encode(piece).decode("ascii"),
    }
