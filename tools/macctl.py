#!/usr/bin/env python3
"""Die Macs von hier aus fragen — ohne RustDesk, ohne VPN, ohne offenen Port.

Legt einen Befehl in der Supabase-Tabelle `mac_commands` ab und wartet auf die
Antwort. Der Mac holt sich den Befehl beim nächsten Blick in den Briefkasten
(alle ~15 s), führt ihn aus und schreibt das Ergebnis in dieselbe Zeile.

    tools/macctl.py list                       # wer meldet sich, mit welcher Version
    tools/macctl.py commands                   # was man schicken kann
    tools/macctl.py mac07 diag-upload          # warum lädt der nichts hoch
    tools/macctl.py mac07 logs --lines 300 --grep "log upload"
    tools/macctl.py mac07,mac22 diag-upload    # mehrere gleichzeitig
    tools/macctl.py all status
    tools/macctl.py mac07 sync --wait 600
    tools/macctl.py result 42                  # Ergebnis später abholen

Zugangsdaten (Dashboard-Supabase, service_role-Key) aus — in dieser Reihenfolge:
    1. MACTOOL_SUPABASE_URL / MACTOOL_SUPABASE_KEY
    2. ~/.mactool/remote.json  ->  {"url": "https://xxx.supabase.co", "key": "..."}

Läuft absichtlich ohne Import aus dem Mac-Code: dieses Skript liegt auf einem
anderen Rechner und soll mit nichts als `requests` auskommen.
"""

import argparse
import getpass
import json
import os
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Fehlt: requests  —  pip3 install requests")

CREDENTIALS_FILE = Path.home() / ".mactool" / "remote.json"
COMMANDS_TABLE = "mac_commands"
AGENTS_TABLE = "mac_agents"

DEFAULT_WAIT_SECONDS = 120
DEFAULT_TTL = "15m"
POLL_INTERVAL_SECONDS = 2
TIMEOUT = 30

# Ab wann gilt ein Mac als abgemeldet (Heartbeat läuft alle 60 s)
STALE_AFTER_SECONDS = 300

DONE_STATES = {"done", "error", "expired"}


# ── Zugang ────────────────────────────────────────────────────────────


def load_credentials() -> tuple[str, str]:
    url = os.getenv("MACTOOL_SUPABASE_URL", "").strip()
    key = os.getenv("MACTOOL_SUPABASE_KEY", "").strip()

    if not (url and key) and CREDENTIALS_FILE.exists():
        try:
            data = json.loads(CREDENTIALS_FILE.read_text())
            url = url or str(data.get("url", "")).strip()
            key = key or str(data.get("key", "")).strip()
        except Exception as e:
            sys.exit(f"{CREDENTIALS_FILE} nicht lesbar: {e}")

    if not (url and key):
        sys.exit(
            "Keine Zugangsdaten gefunden.\n"
            "Entweder:\n"
            "  export MACTOOL_SUPABASE_URL=https://xxx.supabase.co\n"
            "  export MACTOOL_SUPABASE_KEY=<service_role-Key>\n"
            f"oder {CREDENTIALS_FILE} anlegen:\n"
            '  {"url": "https://xxx.supabase.co", "key": "<service_role-Key>"}'
        )

    return url.rstrip("/"), key


class Rest:
    """Gerade so viel PostgREST, wie dieses Werkzeug braucht."""

    def __init__(self, url: str, key: str):
        self.base = f"{url}/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept-Profile": "public",
            "Content-Profile": "public",
        }

    def _request(self, method: str, table: str, **kwargs) -> requests.Response:
        response = requests.request(
            method, f"{self.base}/{table}", headers={**self.headers, **kwargs.pop("headers", {})},
            timeout=TIMEOUT, **kwargs,
        )
        if response.status_code >= 400:
            raise SystemExit(
                f"Supabase antwortete HTTP {response.status_code}: {response.text[:400]}\n"
                f"(Tabellen angelegt? Siehe tools/schema_remote.sql)"
            )
        return response

    def select(self, table: str, params: dict) -> list:
        return self._request("GET", table, params=params).json()

    def insert(self, table: str, row: dict) -> dict:
        response = self._request(
            "POST", table, data=json.dumps(row), headers={"Prefer": "return=representation"}
        )
        rows = response.json()
        return rows[0] if isinstance(rows, list) and rows else rows


# ── Hilfen ────────────────────────────────────────────────────────────


def parse_ttl(text: str) -> timedelta:
    """'15m', '2h', '90s', '1d' → timedelta."""
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    raw = text.strip().lower()
    unit = units.get(raw[-1:])
    if unit is None:
        raise argparse.ArgumentTypeError(f"Ungültige Dauer '{text}' (z.B. 30s, 15m, 2h, 1d)")
    try:
        amount = float(raw[:-1])
    except ValueError:
        raise argparse.ArgumentTypeError(f"Ungültige Dauer '{text}' (z.B. 30s, 15m, 2h, 1d)")
    return timedelta(**{unit: amount})


def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def age_text(timestamp) -> str:
    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return "?"
    seconds = (datetime.now(timezone.utc) - parsed).total_seconds()
    if seconds < 90:
        return f"vor {int(seconds)}s"
    if seconds < 5400:
        return f"vor {int(seconds / 60)}min"
    if seconds < 172800:
        return f"vor {seconds / 3600:.1f}h"
    return f"vor {int(seconds / 86400)}d"


def resolve_targets(rest: Rest, spec: str) -> list[str]:
    """'all' gegen die Heartbeat-Tabelle auflösen, sonst die Kommaliste nehmen."""
    if spec.lower() == "all":
        rows = rest.select(AGENTS_TABLE, {"select": "server_name", "order": "server_name.asc"})
        names = [row["server_name"] for row in rows]
        if not names:
            sys.exit(f"Kein Mac hat sich je gemeldet ({AGENTS_TABLE} ist leer).")
        return names
    return [name.strip() for name in spec.split(",") if name.strip()]


def collect_args(pairs: list[str], namespace) -> dict:
    """Benannte Argumente für den Befehl einsammeln."""
    args = {}

    for name in ("lines", "grep", "file", "dir", "version", "action"):
        value = getattr(namespace, name, None)
        if value is not None:
            args[name] = value
    if getattr(namespace, "dry_run", False):
        args["dry_run"] = True
    if getattr(namespace, "no_upload_all", False):
        args["upload_all"] = False
    if getattr(namespace, "check_updates", False):
        args["check_updates"] = True

    # Alles Weitere als key=value, damit neue Befehle kein CLI-Update brauchen
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"Zusatzargument '{pair}' braucht die Form schluessel=wert")
        key, _, value = pair.partition("=")
        try:
            args[key] = json.loads(value)
        except json.JSONDecodeError:
            args[key] = value

    return args


# ── Unterbefehle ──────────────────────────────────────────────────────


def cmd_list(rest: Rest, namespace) -> int:
    rows = rest.select(AGENTS_TABLE, {"select": "*", "order": "server_name.asc"})
    if not rows:
        print(f"Kein Mac hat sich je gemeldet ({AGENTS_TABLE} ist leer).")
        print("Läuft auf den Macs schon v1.0.115, und ist schema_remote.sql eingespielt?")
        return 1

    if namespace.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    print(f"{'MAC':<16} {'STATUS':<11} {'GESEHEN':<10} {'VERSION':<12} {'BOT':<5} {'RUSTDESK':<9} UHR")
    print("-" * 82)
    stale_found = False
    skewed = []
    for row in rows:
        seen = parse_timestamp(row.get("last_seen"))
        offline = (
            seen is None
            or (datetime.now(timezone.utc) - seen).total_seconds() > STALE_AFTER_SECONDS
        )
        stale_found = stale_found or offline

        # Abweichung zwischen der Uhr des Macs und der der Datenbank
        clock = parse_timestamp(row.get("agent_clock"))
        drift = ""
        if clock and seen:
            delta = (clock - seen).total_seconds()
            if abs(delta) >= 60:
                drift = f"{delta / 60:+.0f} min"
                skewed.append(row.get("server_name", "?"))
            else:
                drift = "ok"

        print(
            f"{row.get('server_name', '?'):<16} "
            f"{'ABGEMELDET' if offline else 'ok':<11} "
            f"{age_text(row.get('last_seen')):<10} "
            f"{(row.get('version') or '?'):<12} "
            f"{('an' if row.get('bot_running') else 'aus'):<5} "
            f"{('an' if row.get('rustdesk_running') else 'aus'):<9} "
            f"{drift}"
        )
    if stale_found:
        print(f"\nABGEMELDET = kein Lebenszeichen seit über {STALE_AFTER_SECONDS // 60} Minuten.")
    if skewed:
        print(
            f"\nUHR = Abweichung der Mac-Uhr von der Datenbank: {', '.join(skewed)}. "
            f"Nur zur Kenntnis — Verfall und Status rechnen mit der Zeit der Datenbank, "
            f"die Systemuhr der Macs darf verstellt bleiben."
        )
    return 0


def cmd_result(rest: Rest, namespace) -> int:
    rows = rest.select(COMMANDS_TABLE, {"id": f"eq.{namespace.command_id}", "select": "*"})
    if not rows:
        sys.exit(f"Kein Befehl mit der ID {namespace.command_id}")
    print(json.dumps(rows[0], indent=2, ensure_ascii=False))
    return 0


def cmd_pending(rest: Rest, namespace) -> int:
    rows = rest.select(
        COMMANDS_TABLE,
        {
            "status": "in.(queued,running)",
            "select": "id,server_name,command,status,created_at,expires_at",
            "order": "created_at.asc",
        },
    )
    if not rows:
        print("Nichts offen.")
        return 0
    for row in rows:
        print(
            f"#{row['id']:<6} {row['server_name']:<14} {row['command']:<16} "
            f"{row['status']:<8} seit {age_text(row['created_at'])}"
        )
    return 0


def send_and_wait(rest: Rest, target: str, command: str, args: dict, namespace) -> dict:
    """Einen Befehl abschicken und auf das Ergebnis warten."""
    expires_at = datetime.now(timezone.utc) + parse_ttl(namespace.ttl)
    row = rest.insert(
        COMMANDS_TABLE,
        {
            "server_name": target,
            "command": command,
            "args": args,
            "expires_at": expires_at.isoformat(),
            "requested_by": f"{getpass.getuser()}@{socket.gethostname()}",
        },
    )
    command_id = row["id"]

    deadline = time.monotonic() + namespace.wait
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        current = rest.select(COMMANDS_TABLE, {"id": f"eq.{command_id}", "select": "*"})
        if current and current[0].get("status") in DONE_STATES:
            return current[0]

    return {
        "id": command_id,
        "server_name": target,
        "command": command,
        "status": "timeout",
        "error": (
            f"Nach {namespace.wait}s keine Antwort. Der Befehl steht weiterhin in der "
            f"Queue — Ergebnis später mit 'macctl.py result {command_id}' abholen. "
            f"Meldet sich der Mac überhaupt? 'macctl.py list'"
        ),
    }


def render(row: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(row, indent=2, ensure_ascii=False))
        return

    status = row.get("status")
    marker = {"done": "OK", "error": "FEHLER", "expired": "ABGELAUFEN", "timeout": "ZEITÜBERLAUF"}
    print(f"\n=== {row.get('server_name')} · {row.get('command')} · "
          f"{marker.get(status, status)} (#{row.get('id')}) ===")

    if row.get("error"):
        print(row["error"])
    result = row.get("result")
    if result is not None:
        # Die Diagnose beantwortet die Frage in einer Zeile — die zuerst.
        if isinstance(result, dict) and result.get("verdict"):
            print(f"\n>> {result['verdict']}\n")
        print(json.dumps(result, indent=2, ensure_ascii=False))


def cmd_send(rest: Rest, namespace) -> int:
    targets = resolve_targets(rest, namespace.target)
    args = collect_args(namespace.extra, namespace)

    # Auf stderr: sonst steht die Zeile mitten im JSON und `| python3 -m json.tool`
    # scheitert an ihr.
    print(
        f"Sende '{namespace.command}' an {', '.join(targets)} "
        f"(Wartezeit {namespace.wait}s, Gültigkeit {namespace.ttl})...",
        file=sys.stderr,
    )

    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
        rows = list(
            pool.map(
                lambda target: send_and_wait(rest, target, namespace.command, args, namespace),
                targets,
            )
        )

    for row in rows:
        render(row, namespace.json)

    return 0 if all(row.get("status") == "done" for row in rows) else 1


def cmd_commands(rest: Rest, namespace) -> int:
    """Was der Mac annimmt — die Liste steht in remote_commands.HANDLERS."""
    print("Lesend:   status  config  logs  files  diag-upload  diag-db  versions  legacy-upload")
    print("          warnings  warning-text name=<zip>  warning-part name=<zip> member=<datei> offset=<n>")
    print("Eingreifend (brauchen remote_allow_actions):")
    print("          sync  cleanup  set  customer-stats  bot  rustdesk  update")
    print()
    print("Beispiele:")
    print("  macctl.py mac07 diag-upload")
    print("  macctl.py mac07 logs --lines 300 --grep 'log upload'")
    print("  macctl.py mac07 files --dir botlogs")
    print("  macctl.py mac07 bot --action restart")
    print("  macctl.py mac07 update --version v1.0.115")
    print("  macctl.py mac07 sync --wait 600")
    print("  macctl.py all cleanup           # alte Logs im Bucket sofort wegraeumen")
    print("  macctl.py mac08 set sync_times=00:10,02:10,04:10,06:10,08:10,10:10,\\")
    print("                        12:10,14:10,16:10,18:10,20:10,22:10")
    print("  macctl.py all set alert_smtp_host=smtp.example.de alert_smtp_port=587")
    print()
    print("Nicht setzbar (bewusst): supabase_key, customer_stats_key,")
    print("customer_users_key, alert_smtp_password, webhook_url, server_name.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Macs über die Supabase-Befehlsqueue ansprechen.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--json", action="store_true", help="Rohausgabe als JSON")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # `--json` soll vor *und* hinter dem Befehl funktionieren. SUPPRESS ist hier
    # wesentlich: ohne das setzt der Unterparser das Feld auf seinen Default
    # zurueck und ueberschreibt damit ein vorangestelltes --json wieder mit False.
    def add_json(target):
        target.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS,
            help="Rohausgabe als JSON",
        )

    list_parser = sub.add_parser("list", help="Welche Macs melden sich (Heartbeat)")
    add_json(list_parser)
    list_parser.set_defaults(func=cmd_list)

    sub.add_parser("commands", help="Verfügbare Befehle anzeigen").set_defaults(func=cmd_commands)
    sub.add_parser("pending", help="Offene Befehle aller Macs").set_defaults(func=cmd_pending)

    result_parser = sub.add_parser("result", help="Ergebnis eines Befehls nachträglich abholen")
    result_parser.add_argument("command_id", type=int)
    add_json(result_parser)
    result_parser.set_defaults(func=cmd_result)

    send = sub.add_parser("send", help="Befehl schicken (auch ohne 'send' aufrufbar)")
    send.add_argument("target", help="mac07 | mac07,mac22 | all")
    send.add_argument("command", help="z.B. status, diag-upload, logs, sync")
    send.add_argument("extra", nargs="*", help="weitere Argumente als schluessel=wert")
    send.add_argument("--wait", type=int, default=DEFAULT_WAIT_SECONDS,
                      help=f"Sekunden auf Antwort warten (Default {DEFAULT_WAIT_SECONDS})")
    send.add_argument("--ttl", type=str, default=DEFAULT_TTL,
                      help=f"Wie lange der Befehl gültig bleibt (Default {DEFAULT_TTL})")
    send.add_argument("--lines", type=int, help="logs: wie viele Zeilen")
    send.add_argument("--grep", type=str, help="logs: nur Zeilen mit diesem Muster")
    send.add_argument("--file", type=str, help="logs: welche Datei (Default mactool.log)")
    send.add_argument("--dir", type=str, choices=["botlogs", "mactool"], help="files: welches Verzeichnis")
    send.add_argument("--action", type=str, help="bot/rustdesk: start, stop, restart")
    send.add_argument("--version", type=str, help="update: Ziel-Tag, z.B. v1.0.115")
    send.add_argument("--dry-run", action="store_true", help="customer-stats: nur vorschauen")
    send.add_argument("--no-upload-all", action="store_true",
                      help="sync: nur das letzte Zeitfenster statt aller Logs")
    send.add_argument("--check-updates", action="store_true",
                      help="status: auch GitHub abfragen (langsamer)")
    add_json(send)
    send.set_defaults(func=cmd_send)

    return parser


def main() -> int:
    parser = build_parser()
    argv = sys.argv[1:]

    # 'macctl.py mac07 status' soll ohne das Wort 'send' funktionieren — das ist
    # der Aufruf, den man hundertmal am Tag tippt.
    known = {"list", "commands", "pending", "result", "send"}
    first = next((a for a in argv if not a.startswith("-")), None)
    if first is not None and first not in known:
        argv.insert(argv.index(first), "send")

    namespace = parser.parse_args(argv)

    # Die Befehlsübersicht ist eine reine Textausgabe — die soll auch dann
    # funktionieren, wenn noch keine Zugangsdaten hinterlegt sind.
    if namespace.subcommand == "commands":
        return cmd_commands(None, namespace)

    return namespace.func(Rest(*load_credentials()), namespace)


if __name__ == "__main__":
    sys.exit(main())
