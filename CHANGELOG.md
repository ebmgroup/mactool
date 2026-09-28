# Changelog

## v1.0.123 — 2026-09-28
- **Neue lesende Befehle für die Warn-Archive des Bots.** GramAddict legt bei Auffälligkeiten
  ein ZIP nach `warnings/` (Screenshot, UI-Abbild, Log-Auszug) — nur dort steht, welches
  Fenster Instagram tatsächlich gezeigt hat; der Bot schreibt den Text nie ins Log
  („Probably block dialog is shown…")
  - `warnings` listet die ZIPs (gesucht bis drei Ebenen unter GramBotStorage, dessen
    Elternordner und dem Home-Verzeichnis)
  - `warning-text name=<zip>` liefert die Bildschirmtexte (Text + Beschreibung aller Knoten)
    und das Log-Ende
  - `warning-part` holt eine Datei stückweise (≤ 150.000 Zeichen je Antwort, die Queue
    kappt bei 200.000); `tools/warning_get.py` setzt sie lokal zusammen
- Nur Dateinamen aus den gefundenen `warnings`-Ordnern, nur `.zip`; nichts wird verändert

## v1.0.122 — 2026-09-08
- **Neuer Befehl `set`: Einstellungen lassen sich aus der Ferne ändern.** Damit ist eine
  Korrektur an 13 Macs ein Befehl statt 13 RustDesk-Sitzungen —
  `macctl.py mac08 set sync_times=00:10,02:10,…`
- Bewusst **nicht** setzbar: `supabase_key`, `customer_stats_key`, `customer_users_key`,
  `alert_smtp_password`, `webhook_url` — Keys und Passwörter bleiben dem Dashboard
  vorbehalten. Ebenso `server_name`: den zu ändern hieße, den Mac von der Fernsteuerung
  abzuschneiden, und zurück ginge es nur noch per RustDesk
- Werte werden vor dem Speichern geprüft: Uhrzeiten auf `HH:MM`, Zahlen auf Zahlen,
  ja/nein auf beides, Aufzählungen auf ihre erlaubten Werte. Ein leerer `sync_times` wird
  abgelehnt, weil danach überhaupt kein Upload mehr liefe
- **Fix: geänderte Upload-Zeiten wirkten erst nach einem Neustart** — auch die über das
  Dashboard. Bis dahin lief still der alte Zeitplan weiter. Die Termine werden jetzt sofort
  neu aufgebaut, gestrichene Zeitpunkte dabei entfernt
- **Die Diagnose prüft jetzt auch den Zeitplan.** Ein Lauf lädt immer nur das zuletzt
  abgeschlossene 2-Stunden-Fenster hoch. Läuft der Sync nicht alle zwei Stunden, fehlen die
  Logs der übersprungenen Fenster dauerhaft — auf mac08 stand der Standardzeitplan
  (09:00, 14:30), also 2 von 12 Fenstern abgedeckt und zehn Fenster für immer verloren
- Solche Fälle erscheinen als `warnings` und stehen mit „ACHTUNG" im Urteil. Vorher meldete
  die Diagnose „kein blockierendes Glied gefunden" — richtig, aber irreführend: die Kette
  war heil, der Zeitplan nicht

## v1.0.121 — 2026-09-08
- Neuer Befehl `cleanup`: stößt die Bucket-Bereinigung sofort an, statt auf den Tageslauf
  zu warten. Ohne den wäre der Rückstand aus dem Zukunftsdaten-Fehler bis zum nächsten Tag
  liegen geblieben — `macctl.py all cleanup`
- Hinweis zur Tag-Hygiene: v1.0.120 war zu diesem Zeitpunkt bereits auf allen Macs
  ausgecheckt. Ein gepushter Tag darf deshalb nicht verschoben werden — `git fetch --tags`
  zieht ihn auf den Macs nicht nach, sie blieben stumm auf dem alten Stand. Nachträge
  bekommen eine neue Nummer

## v1.0.120 — 2026-09-08
- **Fix: hochgeladene Logs bekamen Daten in der Zukunft.** In der Logzeile steht nur
  `[MM/TT HH:MM:SS]`, das Jahr fehlt. Bisher wurde immer das laufende Jahr angenommen und
  nur der Fall Dezember/Januar korrigiert. In den Logverzeichnissen liegen aber Dateien von
  2022 bis heute — auf mac04 sind es 3826. Aus einem Log vom **Dezember 2024** wurde damit
  `2026-12-27`
- Solche Dateien blieben für immer im Bucket liegen: auf ein Datum in der Zukunft greift
  die 3-Tage-Frist nie. Daher 400 bis 800 Objekte je Mac statt der erwarteten paar Dutzend
- Das Jahr wird jetzt so gewählt, dass Tag und Monat am dichtesten am **Änderungsdatum der
  Datei** liegen. Das ist der bessere Anker als „jetzt": Dateidatum und Loginhalt stammen
  von derselben Maschine und sind auch dann zueinander stimmig, wenn deren Systemuhr
  verstellt ist. Der Jahreswechsel ist damit in beide Richtungen abgedeckt, der 29.02. in
  einem Nicht-Schaltjahr wirft nicht mehr
- Die Bereinigung räumt die bereits entstandenen Zukunftsdateien mit weg: ein Datum mehr
  als zwei Tage voraus kann kein gültiger Log sein. Zwei Tage Spielraum, weil die Systemuhr
  der Macs verstellt sein darf

## v1.0.119 — 2026-09-08
- **Der Fernzugriff rechnet jetzt mit der Uhr der Datenbank, nicht mit der des Macs.**
  Auf den Macs darf die Systemuhr bewusst verstellt sein. Bisher wurde das Verfallsdatum
  eines Auftrags gegen die lokale Uhr geprüft — ein Mac, der eine halbe Stunde vorgeht,
  hätte jeden Auftrag mit der 15-Minuten-Frist sofort als „abgelaufen" verworfen und den
  Fernzugriff damit lautlos stillgelegt
- Als gemeinsame Referenz dient der `Date`-Header, den jede Antwort der Datenbank ohnehin
  mitbringt. Fortgeschrieben wird er mit `time.monotonic()` — das läuft weiter, auch wenn
  jemand die Systemuhr mitten im Betrieb verstellt. Betrifft `expires_at`, `claimed_at`
  und `finished_at`
- `agent_clock` bleibt bewusst die **lokale** Uhr des Macs: genau ihre Abweichung soll die
  Spalte sichtbar machen. `macctl.py list` zeigt sie als eigene Spalte an, ohne den Status
  zu verfälschen
- Auf mac07 gemessen: die Uhr geht 7 Minuten nach. Vorher stand der Mac deshalb dauerhaft
  auf „ABGEMELDET", obwohl er sekündlich antwortete

## v1.0.118 — 2026-09-08
- **Fix zu v1.0.117: der Heartbeat braucht das neue SQL nicht mehr zwingend.** v1.0.117
  schickte nur noch `agent_clock` und überließ `last_seen` dem Trigger. War das SQL noch
  nicht eingespielt, kannte die Tabelle die Spalte nicht — der Upsert ließ sie weg und
  `last_seen` wäre beim Aktualisieren für immer auf dem alten Wert stehen geblieben
- Jetzt werden beide Werte geschickt: mit Trigger gewinnt die Zeit der Datenbank, ohne
  Trigger bleibt wenigstens die Mac-Zeit stehen. Das SQL bleibt empfohlen, ist aber keine
  Bedingung mehr dafür, dass sich ein Mac überhaupt meldet

## v1.0.117 — 2026-09-08
- **`last_seen` stempelt jetzt die Datenbank, nicht der Mac.** Auf mac07 geht die Uhr sieben
  Minuten nach — dadurch sah jeder Heartbeat abgelaufen aus, obwohl der Mac gerade eben
  geschrieben hatte. Die Uhr der Datenbank ist die eine Referenz, die alle teilen
- Die Uhr des Macs steht daneben in der neuen Spalte `agent_clock`. `macctl.py list` zeigt
  die Abweichung in einer eigenen Spalte an — damit ist eine schiefe Uhr sichtbar statt
  verwirrend, und man sieht sie überhaupt erst
- **Voraussetzung: `tools/schema_remote.sql` erneut ausführen** (die Datei ist mehrfach
  ausführbar). Sie legt `agent_clock` nach und setzt den Trigger. Wirkt sofort, auch für
  Macs, die noch auf v1.0.115/116 laufen
- `macctl.py`: `--json` funktioniert jetzt auch *hinter* dem Befehl
  (`macctl.py mac07 status --json`), nicht nur davor
- `macctl.py`: die Fortschrittsmeldung geht nach stderr statt in die JSON-Ausgabe — vorher
  scheiterte jedes Weiterverarbeiten mit `| python3 -m json.tool` an dieser einen Zeile

## v1.0.116 — 2026-09-08
- **Fix: der automatische Log-Upload hat seit Monaten nichts hochgeladen — auf allen Macs.**
  Der Abgleich des Zeitfensters war ein reiner Textvergleich: der Code baute `08:00-09:59`
  und fragte, ob diese Zeichenkette im gespeicherten Wert vorkommt. Der Bot schreibt aber
  `08:00-09:57`. **Zwei Minuten** — also nie ein Treffer, für kein Profil, auf keinem Mac
- Der manuelle Knopf „Sync Now" umgeht diesen Filter (`upload_all=True`) und funktionierte
  deshalb weiter. Genau das hat den Fehler verdeckt: wer nachsah, sah Dateien im Bucket
- Verglichen wird jetzt nicht mehr als Text, sondern als Zeitraum: die gespeicherten
  Bereiche werden gelesen und auf Überschneidung mit dem letzten abgeschlossenen
  2-Stunden-Fenster geprüft. Mehrere Bereiche pro Feld (`"08:00-09:57, 20:00-21:57"`),
  Punkt- wie Doppelpunktschreibweise und Bereiche über Mitternacht sind abgedeckt;
  `"run manually"` passt weiterhin zu keinem Fenster
- **Fix: bei nichts zu tun wurde auch nicht mehr aufgeräumt.** Fand der Filter keinen
  Account, stieg `upload_bot_logs` aus, *bevor* die Aufbewahrungsfrist griff. Folge: im
  Bucket lagen trotz 3-Tage-Frist noch Dateien von vor Wochen (auf mac07 vom 30.07.).
  Die Bereinigung läuft jetzt auch dann, wenn nichts hochzuladen ist
- Gefunden mit der Ferndiagnose aus v1.0.115, belegt an den echten Daten von mac07:
  74 freigegebene Accounts, 72 passende Logdateien, alle mit lesbarem Zeitstempel und
  fertigem Zielpfad — und trotzdem null Uploads, weil der Textvergleich scheiterte
- Die Diagnose benutzt jetzt dieselbe Vergleichsfunktion wie der Upload, damit sie nicht
  wieder etwas anderes behaupten kann als der Code tut

## v1.0.115 — 2026-09-08
- **Die Macs lassen sich aus der Ferne befragen.** Bisher war RustDesk der einzige Weg auf
  einen Mac — also Bildschirm gucken statt analysieren, eine Sitzung pro Frage. Jetzt schaut
  jeder Mac alle 15 Sekunden in die Tabelle `mac_commands` der Dashboard-Supabase, arbeitet
  ab was für ihn dort liegt und schreibt die Antwort in dieselbe Zeile
- Kein offener Port, kein VPN, keine neue Software: die Richtung dreht sich um, der Mac
  ruft von sich aus an. `supabase_url` / `supabase_key` liegen ohnehin auf jedem Mac
- **Neu: `tools/macctl.py`** — das Gegenstück für den eigenen Rechner:
  `macctl.py mac07 diag-upload`, `macctl.py mac07,mac22 status`, `macctl.py all status`.
  `macctl.py list` zeigt, welcher Mac sich überhaupt meldet und mit welcher Version
- Ausgeführt wird ausschließlich eine **feste Befehlsliste**, kein freies Shell:
  lesend `status`, `logs`, `config`, `files`, `diag-upload`, `diag-db`, `versions`,
  `legacy-upload` — eingreifend `sync`, `customer-stats`, `bot`, `rustdesk`, `update`
- Der Schalter „Eingreifende Befehle" sperrt die zweite Gruppe; dann sind nur noch Status
  und Diagnose möglich. Beide Schalter stehen im Dashboard unter „Fernzugriff"
- Ein Befehl trägt ein Verfallsdatum (Default 15 Minuten). Ein Mac, der drei Tage aus war,
  arbeitet beim Hochfahren keine überholten Aufträge mehr ab — insbesondere kein `update`
  auf eine Version von vorgestern
- Reserviert wird über ein bedingtes UPDATE. Laufen versehentlich zwei mactool-Prozesse,
  führt trotzdem genau einer den Befehl aus
- Geheimnisse verlassen den Mac nie im Klartext: `config` maskiert Supabase-Keys,
  SMTP-Passwort **und** die `webhook_url` — deren ID ist das Geheimnis
- **Neu: Log-Upload-Diagnose.** Der Bucket-Upload ist eine Kette aus sieben Filtern; fällt
  einer auf null, passiert nichts — ohne Fehler, ohne Meldung. Die Diagnose geht die Kette
  Glied für Glied ab und nennt das erste, das blockiert: fehlender Key, beschädigte oder
  fehlende super.db, leeres Logverzeichnis, kein Gerät mit „Phone" im Namen, kein Profil im
  aktuellen Zeitfenster, Dateiname passt zu keinem Account, unlesbarer Zeitstempel,
  unerreichbarer Bucket. Dazu der Bucket-Inhalt mit Datum: hat es je funktioniert, und
  wann hörte es auf
- Die Diagnose weist ausdrücklich auf die Punkt/Doppelpunkt-Falle hin: stehen die
  Zeitfenster als `00.00-23.59` in der Datenbank, verglichen wird aber gegen `08:00-09:59`,
  kann der Textvergleich nie zutreffen — der automatische Sync lädt dann nie etwas hoch,
  der Knopf „Sync Now" dagegen schon
- Erreichbar im Dashboard („Log-Upload analysieren"), über die Ferne (`diag-upload`) und
  auf der Kommandozeile (`--diag-upload`, `--diag-db`). Rein lesend: lädt nichts hoch,
  speichert nichts, verschickt keine Mail
- Jeder Mac trägt sich einmal pro Minute in `mac_agents` ein. Damit ist ohne einen einzigen
  Befehl sichtbar, welcher Mac läuft, auf welcher Version und ob Bot und RustDesk an sind
- Voraussetzung: einmalig `tools/schema_remote.sql` im SQL-Editor der Dashboard-Supabase
  ausführen. RLS ist an, ohne Policy — nur der service_role-Key kommt an die Tabellen
- Aufräumen nebenbei: der Zustandsbericht (`status.py`) und die Maskierung (`config.py`)
  haben jetzt je eine Quelle statt zwei, die auseinanderdriften konnten

## v1.0.114 — 2026-08-12
- **Vor dem Supabase-Sync wird die super.db geprüft.** `PRAGMA integrity_check` läuft auf dem
  Original, *bevor* die Arbeitskopie gezogen wird. Ist die Datei beschädigt, findet **kein**
  Upload statt — bisher wären beschädigte oder unvollständige Daten nach Supabase gewandert
- **E-Mail-Alarm bei beschädigter Datenbank** an `development@ebm-group.de` (einstellbar):
  „Auf macXX war der Integrity Check der Datenbank fehlerhaft", dazu Pfad, Zeitpunkt und die
  Meldung von SQLite. Versand per SMTP direkt vom Mac, Zugangsdaten im Dashboard
- Keine Mailflut: eine Mail je Störung, danach frühestens nach der eingestellten Wiederholsperre
  (Default 6 h); eine erfolgreiche Prüfung setzt den Zähler zurück. Konnte die Mail nicht
  zugestellt werden, wird es beim nächsten Lauf erneut versucht
- Eine **fehlende** Datenbank ist kein Defekt und löst keine Mail aus — das bleibt `no_db`
- Dashboard: neuer Block „Datenbank-Prüfung + E-Mail-Alarm" mit „Testmail senden" und
  „Jetzt prüfen"; das SMTP-Passwort wird wie die Supabase-Keys nur maskiert ausgeliefert
- **Fehlende Xcode Command Line Tools werden im Klartext gemeldet.** Ohne sie ist `git` auf
  macOS nur eine Attrappe: das Dashboard zeigte „Version: unknown" und das Update brach mit
  „git reset failed: xcrun: error…" ab. Jetzt steht da, was zu tun ist (`xcode-select --install`)

## v1.0.113 — 2026-08-10
- **Fix: der Kunden-Upload schrieb ins falsche Schema.** PostgREST löst eine Anfrage ohne
  Schema-Angabe gegen das *zuerst freigegebene* Schema auf. In der Kunden-Supabase ist das
  `api` — dort liegt eine schmale Lese-View `api.users` mit 15 Spalten, auf die `anon` nur
  SELECT hat. Die echte Tabelle ist `public.users` mit 21 Spalten
- `supabase-py` (das Altskript) sendet `Accept-Profile`/`Content-Profile: public` bei jedem
  Request, unser REST-Helper tat das nicht — daher `42501 permission denied for view users`
- Beide Header werden jetzt gesendet, Schema je Ziel einstellbar
  (`customer_stats_schema` / `customer_users_schema`, Default `public`)
- Damit klärt sich auch die Spalten-Frage aus v1.0.110 endgültig: `device`, `total_watched`,
  `total_scraped` und `serverzuordnung` fehlen nicht in der Tabelle, sondern nur in der View
- Verbindungstest zeigt das Schema mit an (`public.users` statt nur `users`)

## v1.0.112 — 2026-08-10
- **Fehlende Schreibrechte brechen den Upload sofort ab statt jede Zeile einzeln zu
  versuchen.** Auf mac17 lief Ziel B in 946 Einzelversuche mit identischem Fehler
  (`42501 permission denied for view users`) — jetzt genügt ein Request für die Erkenntnis
- Dashboard benennt den Fall im Klartext: „keine Schreibrechte auf 'users'" plus die drei
  Auswege (INSERT-Recht vergeben, Key mit Schreibrecht, oder echte Tabelle statt View)
- Der Verbindungstest sagt jetzt dazu, dass er **nur den Lesezugriff** prüft — Schreibrechte
  zeigen sich erst beim Upload
- Ein abgebrochenes Ziel gilt als `error`, nicht mehr als `partial`

## v1.0.111 — 2026-08-10
- **Fix: ein Ziel ohne URL/Key wurde stumm übersprungen und der Lauf trotzdem als
  „success" gemeldet.** Auf mac17 war nur Ziel A (statistik) hinterlegt — Ziel B (users)
  bekam dadurch seit dem Umstieg nichts, ohne dass es irgendwo sichtbar war
- Neuer Status `incomplete`: mindestens ein Ziel läuft, ein anderes ist nicht konfiguriert
- Das alte Upload-LaunchAgent wird nur noch deaktiviert, wenn der Lauf wirklich `success`
  ist (also **alle** Ziele konfiguriert und fehlerfrei) **und** „Statistik (Kunde)"
  eingeschaltet ist — sonst würde das Altskript abgeschaltet, ohne dass etwas es ersetzt
- Dashboard zeigt je Ziel dauerhaft den Zustand an: „Kein Key hinterlegt — wird
  übersprungen", bzw. Zeitpunkt und Zeilenzahl des letzten Laufs. Bisher stand das nur in
  der Meldung direkt nach einem Lauf
- Das Ergebnis je Ziel wird in `run_state.json` mitprotokolliert
- LaunchAgent-Erkennung findet das Skript jetzt auch, wenn es in einem Shell-Aufruf steckt
  (`/bin/sh -c "cd ... && python3 upload-mac17.py"`) — vorher wurde nur ein direkter
  Skriptpfad erkannt. Ein echter `upload-*.py`-Dateiname bleibt Voraussetzung

## v1.0.110 — 2026-08-06
- **Fix: keine stillen Datenverluste mehr beim Kunden-Upload.** Die Spaltenerkennung fiel,
  wenn ein Key die Tabellenstruktur nicht auslesen darf (anon-Key, OpenAPI antwortet 401),
  auf eine Zeilen-Stichprobe zurück. Die zeigt aber nur Spalten, die der Key **lesen** darf —
  bei `users` sind das 15 von 19. `device`, `total_watched`, `total_scraped` und
  `serverzuordnung` wären dadurch aus jeder Zeile entfernt worden, ohne Fehlermeldung
- Spalten werden jetzt nur noch anhand der OpenAPI-Spec vorgefiltert (echtes Schema).
  Ist die nicht lesbar, werden alle Felder gesendet
- Eine Spalte wird nur noch entfernt, wenn die Datenbank sie tatsächlich ablehnt
  (PGRST204 / 42703) — dann für den restlichen Upload, und der Lauf meldet welche
- Verbindungstest unterscheidet „fehlt wirklich" (Status `incomplete`) von „nicht prüfbar,
  weil der Key nicht introspizieren darf" (Status `unverified`) statt fälschlich Alarm zu geben
- Zeilenzahl im Verbindungstest via `count=planned` statt `count=exact` — bei grossen
  Tabellen lief der exakte Count in einen 500er (Anzeige „? Zeilen")

## v1.0.109 — 2026-08-06
- **Statistik (Dashboard)**: An/Aus-Schalter für den bestehenden Upload. Betrifft nur die
  `stats`-Tabelle — `device`, `profile`, `bin` und die Bot-Logs laufen unabhängig weiter
- **Statistik (Kunde)**: Der Upload aus `upload-macXX.py` läuft jetzt im Mactool. Zwei Ziele,
  jedes einzeln konfigurierbar: `statistik` (Rohdaten, Upsert auf `session_id`) und `users`
  (verschönerte Zahlen + Missing-Session-Einträge). Verschönerungs- und Missing-Logik
  unverändert aus dem Altskript übernommen
- `serverzuordnung` kommt aus `server_name` — kein eigenes Skript je Mac mehr nötig
- Datenquelle für den Kunden-Upload umschaltbar: `sessions.json` (Standard) oder die
  `stats`-Tabelle der super.db. Im super.db-Modus bleiben `successful_interactions`, `posts`,
  `total_scraped`, `args` und `profile` leer (Hinweis im Dashboard)
- Beide Uploads manuell auslösbar — unabhängig davon, ob der jeweilige Schalter an ist
- Automatisch laufen beide nacheinander zu den bestehenden `sync_times`
- Dashboard-Karte „Statistik-Uploads": URL, Key (maskiert) und Tabellenname für beide
  Statistiken frei einstellbar; leeres Key-Feld = unverändert, `-` = löschen
- „Verbindung testen" je Ziel: prüft Erreichbarkeit, listet fehlende Spalten und erzeugt das
  passende `ALTER TABLE`-SQL (Spalten anlegen geht über die Supabase-REST-API nicht)
- „Vorschau" zeigt die fertig gemappten Daten, ohne etwas hochzuladen
- Batch-Upload statt ~30.000 Einzelrequests pro Lauf, mit Retry/Backoff bei 429/5xx und
  zeilenweisem Fallback, wenn ein Batch kippt
- Missing-Session-Einträge max. 1× pro Tag und Account (das Altskript lief 1× nachts, das
  Mactool läuft mehrmals täglich)
- Altes Upload-LaunchAgent wird erkannt und nach dem ersten erfolgreichen Kunden-Upload
  automatisch deaktiviert (nur Umbenennen, jederzeit reversibel); Buttons im Dashboard
- Letzter Lauf je Upload wird protokolliert und im Dashboard angezeigt
- CLI: `--customer-stats` und `--customer-stats-preview`
- `config.json` mit unbekannten Feldern führt nicht mehr zum Absturz (Downgrade auf eine
  ältere Version über die Versionsauswahl)
- Sync erkennt geänderte Supabase-Einstellungen ohne Neustart
- Neue Abhängigkeit: PyYAML (mit Fallback-Parser, falls die Installation fehlschlägt)

## v1.0.108 — 2026-06-02
- RustDesk-Watchdog: stellt sicher, dass RustDesk.app immer läuft; startet sie automatisch neu, wenn sie geschlossen wurde (Standard-Intervall: alle 5 Min)
- Dashboard: RustDesk-Status (läuft/läuft nicht) + Watch-Status (aktiv/deaktiviert)
- Dashboard-Buttons: "Start RustDesk" (startet sofort + aktiviert Watchdog) und "Disable RustDesk Watch" (deaktiviert nur den Watchdog, schließt RustDesk NICHT)
- Watchdog wird beim Mactool-Start einmalig sofort ausgeführt
- Neue Config-Felder: `rustdesk_app_path` (Default `/Applications/RustDesk.app`), `rustdesk_check_interval_minutes` (Default 5)

## v1.0.107 — 2026-04-07
- Log-Retention von 90 auf 3 Tage reduziert
- Log-Cleanup läuft nur noch 1x pro Tag (nicht mehr bei jedem Sync)
- Log-Upload Throttling verschärft (5er Batches, 0.5s/5s Pausen)
- Rate-Limit Retry für einzelne Log-Uploads (1 Retry bei 429/503/Timeout)
- "Sync Now" Button lädt ALLE Phone-Logs hoch (nicht nur den letzten Timeslot); Auto-Sync bleibt unverändert
- device_monitor.py: Singleton Supabase-Client statt create_client() pro Aufruf
- device_monitor.py: adb_status Updates gebatched (max. 2 Requests statt 1 pro Device)
- sync.py: Bin-Table Stale-Cleanup batched via .in_() statt Loop

## v1.0.101 — 2026-03-25
- Versionierung: Git-Tags statt SHA, CHANGELOG.md
- Dashboard: Versionsanzeige mit Tag-Namen (z.B. v1.0.100)
- Dashboard: Version-Dropdown zum Wechseln auf ältere Versionen
- Update-System: Tag-basiert statt origin/main

## v1.0.100 — 2026-03-25
- Batched Webhook (ein gesammelter Webhook pro Monitor-Durchlauf)
- Reported-Status im Dashboard mit Reset-Button
- Supabase adb_status Spalte für externe Statusabfrage
- Externer Reset via Supabase (adb_status auf "online" setzen)
- Device Restart (einzeln + Restart All) via ADB reboot
- Bot Start/Stop mit Auto-Restart und Login-Shell für ADB-Zugriff
- Service-Restart nach Update via launchd KeepAlive
- Self-Update von GitHub mit Update-Button
- SQLite zu Supabase Sync Engine mit Schema-Discovery
- Device Monitor mit Webhook-Benachrichtigung bei Offline-Transition
- Blacklist-Verwaltung im Dashboard
- Multi-Mac Isolation (Supabase ID-Prefix Filterung)
