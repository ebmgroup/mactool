#!/usr/bin/env python3
"""Eine Datei aus einem Warn-ZIP eines Macs holen und lokal zusammensetzen.

    tools/warning_get.py mac05 3.7.9b0_2026-09-27-04-50-49.zip screenshot.png [ziel]

Schickt so oft `warning-part`, bis die Datei vollständig ist (je Stück etwa 110 KB,
jedes dauert wegen des Poll-Intervalls rund 15 Sekunden). Welche Dateien im ZIP
liegen, zeigt vorher `macctl.py mac05 warning-text name=<zip>`.
"""
import base64
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    mac, name, member = sys.argv[1:4]
    ziel = Path(sys.argv[4] if len(sys.argv) > 4 else f"{mac}_{Path(name).stem}_{Path(member).name}")
    teile, offset = [], 0
    while offset is not None:
        out = subprocess.run(
            [sys.executable, str(HERE / "macctl.py"), mac, "warning-part", f"name={name}",
             f"member={member}", f"offset={offset}", "--json", "--wait", "180"],
            capture_output=True, text=True,
        )
        try:
            antwort = json.loads(out.stdout)
        except json.JSONDecodeError:
            print(out.stdout, out.stderr, sep="\n")
            return 1
        zeile = antwort[0] if isinstance(antwort, list) else antwort
        r = zeile.get("result") if isinstance(zeile, dict) else None
        if not isinstance(r, dict) or "data_b64" not in r:
            print(json.dumps(zeile, ensure_ascii=False, indent=2)[:2000])
            return 1
        teile.append(base64.b64decode(r["data_b64"]))
        offset = r["next_offset"]
        print(f"  {sum(map(len, teile))} / {r['total_bytes']} Bytes")
    ziel.write_bytes(b"".join(teile))
    print(f"gespeichert: {ziel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
