# Fernzugriff auf die Macs

Die Macs stehen hinter NAT — von außen kommt niemand herein. Also dreht sich die Richtung
um: **jeder Mac schaut alle 15 Sekunden selbst nach**, ob in der Supabase-Tabelle
`mac_commands` ein Auftrag für ihn liegt, führt ihn aus und schreibt die Antwort in
dieselbe Zeile.

Kein offener Port, kein VPN, keine zusätzliche Software. RustDesk braucht man nur noch für
das, was der Fernzugriff bewusst nicht kann (siehe unten).

```
hier: tools/macctl.py        Supabase                  Mac
        |                       |                       |
        |--- Auftrag ---------> [mac_commands]          |
        |                       |<-- alle 15 s: was liegt für mich?
        |                       |                       |-- führt aus
        |                       |<-- Ergebnis ----------|
        |<-- holt Ergebnis -----|                       |
        |                       [mac_agents] <-- Lebenszeichen 1x/min
```

---

## Schnellstart

```bash
cd ~/wtf/ebm/mactool
python3 tools/macctl.py list
```

Zeigt das eine Tabelle mit Macs, funktioniert alles. Sonst siehe [Wenn es klemmt](#wenn-es-klemmt).

---

## Einrichtung

### 1. Tabellen anlegen (einmalig pro Supabase-Projekt)

`schema_remote.sql` im SQL-Editor der **Dashboard-Supabase** ausführen — dasselbe Projekt,
das in `config.json` unter `supabase_url` steht. Die Datei ist mehrfach ausführbar.

### 2. Zugangsdaten hinterlegen (einmalig pro Rechner)

`~/.mactool/remote.json`:

```json
{
  "url": "https://xxx.supabase.co",
  "key": "sb_secret_…"
}
```

```bash
chmod 600 ~/.mactool/remote.json
```

Alternativ `MACTOOL_SUPABASE_URL` und `MACTOOL_SUPABASE_KEY` als Umgebungsvariablen.

> **Es muss ein Secret-Key sein.** Auf beiden Tabellen ist RLS aktiv, ohne Policy — nur ein
> `sb_secret_…` (bzw. der alte `service_role`-Key) umgeht das. Ein `sb_publishable_…`
> scheitert mit `401 Invalid API key` oder meldet die Tabellen als nicht vorhanden.
> Zu finden unter Project Settings → API Keys → Secret keys, mit dem Kopier-Knopf.

### 3. Macs vorbereiten (einmalig pro Mac)

Der Mac muss **einmal** per RustDesk über sein Dashboard auf ≥ v1.0.115 aktualisiert
werden. Danach meldet er sich von allein, und jedes weitere Update geht aus der Ferne.

---

## Befehle

Ziel ist ein Mac (`mac07`), eine Liste (`mac07,mac22`) oder `all`.

```bash
tools/macctl.py list                     # wer meldet sich: Version, Bot, RustDesk, Uhr
tools/macctl.py commands                 # Übersicht (ohne Zugangsdaten nutzbar)
tools/macctl.py pending                  # offene Aufträge aller Macs
tools/macctl.py result <id>              # Ergebnis nachträglich abholen
```

### Lesend

Laufen immer, auch wenn eingreifende Befehle gesperrt sind.

| Befehl | wofür |
|---|---|
| `status` | Version, Jobs, letzte Läufe, Bot/RustDesk |
| `config` | ganze Konfiguration, Geheimnisse maskiert |
| `logs --lines 300 --grep "muster"` | Ende von `logs/mactool.log`, optional gefiltert |
| `files --dir botlogs\|mactool` | Verzeichnisinhalt mit Größe und Datum |
| `diag-upload` | **warum landen keine Logs im Bucket** — die ganze Kette |
| `diag-db` | super.db-Integrität und Zeilenzahlen je Tabelle |
| `versions` | verfügbare Versions-Tags |
| `legacy-upload` | Status der alten `upload-macXX.py`-LaunchAgents |
| `warnings --grep 2026-09-27 [art=crash]` | Warn- und Crash-ZIPs des Bots (`warnings/`, `crashes/`), neueste zuerst |
| `warning-text name=<zip>` | Inhalt des ZIPs + **Bildschirmtexte** aus dem UI-Abbild — was Instagram gezeigt hat |
| `warning-part name=<zip> member=<datei> offset=<n>` | ein Stück einer Datei (base64); ganz holen mit `tools/warning_get.py` |

### Eingreifend

Brauchen `remote_allow_actions` (ab Werk an, im Dashboard unter „Fernzugriff" abschaltbar).

| Befehl | wofür |
|---|---|
| `sync` | Sync + Log-Upload. `--no-upload-all` prüft den **automatischen** Pfad |
| `cleanup` | alte Logs im Bucket sofort wegräumen (sonst nur einmal täglich) |
| `set feld=wert` | Konfiguration ändern |
| `customer-stats [--dry-run]` | Kunden-Upload |
| `bot --action start\|stop\|restart` | Bot steuern |
| `rustdesk --action start\|stop` | `stop` schaltet nur den Wächter ab, beendet kein RustDesk |
| `update --version v1.0.122` | Version wechseln; der Dienst startet danach neu |
| `archive-upload` | Warn-/Crash-ZIPs der letzten 7 Tage jetzt hochladen (sonst nach jedem Sync) |
| `archive-cleanup [ausfuehren=true]` | ZIPs älter als 60 Tage auf dem Mac löschen — ohne `ausfuehren=true` nur Vorschau |

### Optionen

| Option | Bedeutung |
|---|---|
| `--wait <s>` | wie lange auf die Antwort gewartet wird (Default 120) |
| `--ttl 30m` | wie lange der Auftrag gültig bleibt (Default 15m) |
| `--json` | Rohausgabe; funktioniert vor *und* hinter dem Befehl |

Läuft die Wartezeit ab, bleibt der Auftrag stehen und wird trotzdem ausgeführt — die ID
wird ausgegeben, `macctl.py result <id>` holt das Ergebnis nach.

---

## Was der Fernzugriff bewusst nicht kann

Dafür weiterhin RustDesk und das Dashboard:

- **Kein freies Shell.** Nur die Liste oben; alles andere antwortet mit „Unbekannter Befehl".
- **Keine Keys und Passwörter setzen**: `supabase_key`, `customer_stats_key`,
  `customer_users_key`, `alert_smtp_password`, `webhook_url`. Lesen geht nur maskiert.
- **Kein `server_name` ändern.** Darüber holt der Mac seine Aufträge ab — wer ihn ändert,
  schneidet den Mac von der Fernsteuerung ab.

Jeder Auftrag steht mit Zeitstempel und Absender nachvollziehbar in `mac_commands`.

---

## Typische Handgriffe

```bash
# Warum lädt ein Mac keine Logs hoch?
tools/macctl.py mac07 diag-upload
```

Die Antwort beginnt mit einer Zeile Klartext (`verdict`). Dazu zwei Listen:
`blocking` — die Kette ist unterbrochen, es passiert gar nichts.
`warnings` — die Kette ist heil, aber unvollständig (etwa ein Zeitplan, der nicht alle
Fenster abdeckt). Beides landet im `verdict`, damit ein halb kaputter Mac nicht gesund aussieht.

```bash
# Ganze Flotte auf eine Version heben
tools/macctl.py all update --version v1.0.122

# Zeitplan geraderücken — muss alle zwölf 2h-Fenster abdecken
tools/macctl.py mac08 set sync_times=00:10,02:10,04:10,06:10,08:10,10:10,12:10,14:10,16:10,18:10,20:10,22:10

# Log durchsuchen
tools/macctl.py mac07 logs --lines 400 --grep "error|failed|Traceback"

# Prüfen, ob wirklich hochgeladen wird (automatischer Pfad, nicht der manuelle)
tools/macctl.py mac07 sync --no-upload-all --wait 600
```

> **Zum Zeitplan:** ein Sync lädt immer nur das *zuletzt abgeschlossene* 2-Stunden-Fenster
> hoch. Deckt `sync_times` nicht alle zwölf Fenster ab, fehlen die Logs der übersprungenen
> Stunden dauerhaft. `diag-upload` prüft das mit.

---

## Wenn es klemmt

| Symptom | Ursache | Abhilfe |
|---|---|---|
| `401 Invalid API key` | falscher Key oder falsches Projekt | Secret-Key aus der Dashboard-Supabase |
| `PGRST205 Could not find the table` | Key ohne Rechte — **nicht** fehlende Tabelle | Secret-Key statt publishable |
| „Kein Mac hat sich je gemeldet" | SQL nicht eingespielt, oder Macs zu alt | `schema_remote.sql` ausführen; Macs auf ≥ v1.0.115 |
| Mac steht auf `ABGEMELDET` | kein Lebenszeichen seit über 5 Minuten | Läuft der Dienst? Sonst per RustDesk nachsehen |
| Auftrag bleibt auf `running` | Befehl läuft noch — ein voller Sync dauert bis zu 20 Minuten | `macctl.py result <id>`; ein Mac arbeitet immer nur einen Auftrag gleichzeitig |
| Auftrag wird `expired` | Mac war offline, Frist abgelaufen | mit `--ttl 2h` erneut schicken |
| Antwort dauert ~15 Sekunden | normal, das ist das Poll-Intervall | — |

### Zur Spalte „UHR"

`macctl.py list` zeigt, wie weit die Systemuhr eines Macs von der Datenbank abweicht.
Das ist **nur eine Information** — die Systemuhren dürfen verstellt sein. Verfall,
Zeitstempel und der Online-Status rechnen alle mit der Zeit der Datenbank, nicht mit der
des Macs. Ohne das würde ein Mac, dessen Uhr vorgeht, jeden Auftrag sofort als
„abgelaufen" verwerfen und wäre lautlos unerreichbar.

---

## Dateien

| Datei | Rolle |
|---|---|
| `tools/macctl.py` | das Werkzeug auf **deinem** Rechner (nur `requests` nötig) |
| `tools/schema_remote.sql` | die beiden Tabellen plus Trigger |
| `remote_agent.py` | die Poll-Schleife auf dem Mac |
| `remote_commands.py` | die erlaubte Befehlsliste |
| `diagnostics.py` | die Log-Upload-Diagnose, rein lesend |
