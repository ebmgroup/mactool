# IG Versionen — Dashboard-Steuerpult + automatische Updates (ab v1.0.138)

Auftrag (Andreas, 14.10.): Menuepunkt "IG Versionen" unter /growth/. Admins/Superadmins laden
APKs hoch und markieren eine als Standard. Pro Mac alle Phones (Name, Device-ID, Version je
Clone): Standard = gruen, aelter = rot + Update-Button. Buttons: je App, je Phone (alle Clones),
je Mac (alles). Updates darf jeder MA ausloesen. Ausfuehrung ueber das MacTool.

## Architektur
- Kontroll-DB `fxreaveeihaawkusmybi` (dort reden die Macs): Tabellen `ig_versions`,
  `ig_phones`, `ig_update_jobs` (tools/schema_ig.sql). RLS an, ohne Policy.
- Edge Function `ig-versionen` im Kontroll-Projekt (Code im mactool-Repo): prueft den
  Dashboard-Login (JWT der Haupt-DB) + Rolle aus `user_roles`; schreibt mit Service-Key.
  Upload: Function vergibt signierte Upload-URLs je 40-MB-Teil (Bucket `apk`), Browser laedt
  direkt, Function schliesst ab (manifest.json + Zeile in ig_versions).
- Mac: eigener Hintergrund-Job `ig_worker` (nicht die Befehlsqueue — ein Mac-Update dauert
  Stunden): meldet Bestand nach `ig_phones` (alle 30 min + nach jedem Auftrag), arbeitet
  `ig_update_jobs` ab — Handy fuer Handy, Original zuerst, dann App Cloner je Kopie.
- Dashboard: Seite /growth/ig-versionen, Menuepunkt in BOARD_MENU.growth, PR nach `beta`.

## Schritte
- [ ] schema_ig.sql + einspielen
- [ ] Mac: clone_update (App-Cloner-Ablauf), ig_worker, Scheduler, Tests
- [ ] Edge Function + Deploy + Secrets
- [ ] Dashboard-Seite, APK-Pruefung im Browser, Tests, PR nach beta
- [ ] mac17 ausrollen, Ende-zu-Ende-Test auf 428, dann alle Macs

---

# Instagram-Rollout auf die Handys — Plan (ab v1.0.129)

Ziel: Eine bereitgestellte Instagram-Version samt App-Cloner-Kopien per adb auf alle
Handys bringen, ohne Logins zu verlieren. Testmac: **mac17** (dort noch nichts gemacht).

## Fakten (mit Andreas geklaert)
- Handys: Xiaomi Redmi + Samsung. Samsung: Instagram vorinstalliert -> Updates deinstallieren.
- Ziel ist immer *unsere* bereitgestellte Version, nicht die neueste. Auto-Update ist aus.
- Kopien: `com.instagram.androie`..`androil` (App Cloner #1..#8), Original `com.instagram.android`.
- Auf dem Original liegen nach Umzug von storyfischer_ontour keine aktiven Accounts auf echten Handys.
- Update ohne Loeschen scheitert von Hand oft — Grund unbekannt.

## Schritt 1: `apps` (nur lesend)  <- jetzt
- [x] `phone_apps.py`: pro Handy Modell, Android, ABI, Xiaomi-USB-Install-Schalter;
      pro Instagram-Paket + App Cloner: versionName/-Code, Installer, System/Update-Flag,
      Anzahl Split-APKs, Signatur-Kennung
- [x] Fernbefehl `apps` (lesend) + Hilfetext in macctl
- [x] Test mit Fake-adb (tests/test_apps.py), in run_all.sh
- [x] v1.0.129 taggen, **nur mac17** updaten, `macctl.py mac17 apps` auswerten
  Ergebnis: 20x Samsung A04s; Kopien einheitlich signiert (= App-Cloner-Schluessel), je 1 APK;
  Original mit 2 verschiedenen Signaturen, 15 Handys am 08./09.10. auf 450.0.0.50.77 aktualisiert.
  Vorbestehend rot (nicht von hier): test_parity.py, test_flow.py

## Schritt 2: Test-Installation auf mac17 (eingreifend) — vorher Rueckfrage
Regel (Andreas): live **Handy fuer Handy**, nie mehrere auf einmal. Der spaetere
Installationsbefehl verlangt genau eine `serial`.
- [ ] Auf je einem Redmi und Samsung: `adb install -r` eurer Version, Fehlercode notieren
- [ ] Kopie: Signatur-Vergleich, Test-Update einer Kopie, Login pruefen

## Schritt 2 Ergebnis (14.10., 428)
- [x] Original 445 per `apk-original` auf 428 (Downgrade -> Updates deinstalliert -> OK)
- [x] App Cloner per `ui` bedient, Klon #1 gebaut -> MISSING_SPLIT (APKM-Bundle als Quelle)
- [ ] **Andreas: einzelne Voll-APK (kein Bundle), arm64-v8a, nodpi, Instagram 445.0.0.45.83**
- [ ] Damit Klon #1 auf 428 wiederholen, Login pruefen
- [ ] `clone-update` automatisieren (Ablauf siehe Memory project_instagram_rollout)

## Schritt 3: Verteilen auf die anderen Handys — nach Schritt 2

---

# Statistik-Uploads im Mactool — Plan (v1.0.109)

Ziel: Zwei getrennt schaltbare Statistik-Uploads im Mactool, beide manuell triggerbar,
beide mit frei konfigurierbarer Supabase (URL sichtbar, Key maskiert, Tabelle wählbar).
Das bisherige plist-Skript `upload-macXX.py` wird dadurch überflüssig.

## Entscheidungen (mit Andreas abgestimmt)
- **Statistik (Dashboard)** = Toggle betrifft **nur die `stats`-Tabelle**. `device`, `profile`,
  `bin` und der Bot-Log-Upload laufen unabhängig weiter.
- **Statistik (Kunde)** = beide Ziele des Altskripts, jedes einzeln konfigurierbar:
  - Ziel A `statistik` (Rohdaten, UPSERT auf `session_id`)
  - Ziel B `users` (verschönerte Zahlen + Missing-Session-Einträge, INSERT mit Dublettenprüfung)
- **Zeitplan**: beide hängen an den bestehenden `sync_times`, nacheinander (kein zweiter Zeitplan).
- **Manuelle Buttons**: laufen immer — egal ob der jeweilige Toggle an oder aus ist.
- **Datenquelle Kunde**: Umschalter `sessions` (Default, wie bisher) ⇄ `super.db`.
  Beim Umschalten auf super.db erscheint ein Hinweis, welche Felder dort leer bleiben.
- **Spalten**: kein Auto-Anlegen (per PostgREST kein DDL möglich) → „Verbindung testen"
  zeigt fehlende Spalten + generiert `ALTER TABLE`-SQL. Upload überspringt fehlende Spalten.
- **Keys**: URLs + Tabellennamen als Default im Repo, Keys leer (service_role gehört nicht in Git).
- **Altes plist-Skript**: wird vom Mactool erkannt und **nach dem ersten erfolgreichen**
  Kunden-Upload automatisch deaktiviert (reversibel), plus Buttons im Dashboard.

## Recherche-Ergebnisse (verifiziert)
- Alle 13 `upload-macXX.py` sind identisch bis auf `serverzuordnung` → `config.server_name` ersetzt das.
- Zielschema `statistik` (eesha…, 20 Spalten) exakt bestätigt: id, username, session_id, start_time,
  total_interactions, successful_interactions, total_followed, total_likes, total_unfollowed,
  total_pm, total_watched, device, posts, followers, following, total_scraped(jsonb), args(jsonb),
  profile(jsonb), imported_at, serverzuordnung.
- Zielschema `users` (apltf…) bleibt unverändert — Payload wird 1:1 aus dem Altskript übernommen.
- `super.db.stats` Spalten (über die gesyncte EBM-Supabase verifiziert): id, profileID, date,
  dateTime ("HH:MM:SS YYYY-MM-DD"), follow ("13/50"), unfollow, like, comment, dm, watch,
  interaction, followers ("nill" möglich), followings, blocked, source_username,
  followBlocked, likeBlocked, logname.
- sessions.json-Schema bestätigt: id, total_*, total_scraped(dict), start_time, args(dict mit
  `device`/`username`), profile(dict mit posts/followers/following).

## Schritte
- [x] `config.py`: neue Felder + `load()` gegen unbekannte Keys härten (Downgrade-Sicherheit)
      - `dashboard_stats_enabled` (True), `dashboard_table_{device,profile,stats,bin}`
      - `customer_stats_enabled` (False), `customer_stats_source` ("sessions"), `customer_stats_session_limit` (90)
      - `customer_stats_{url,key,table}` (Ziel A), `customer_users_{url,key,table}` (Ziel B)
      - `auto_disable_legacy_upload` (True)
- [x] `config.json.example`: neue Felder mit URLs/Tabellen vorbelegt, Keys leer
- [x] `supabase_rest.py` (neu): schlanker REST-Helper (kein zweiter `create_client`)
      — `columns()`, `select_in()`, `insert_many()`, `upsert_many()`, Retry/Backoff bei 429/5xx/Timeout
- [x] `customer_stats.py` (neu):
      - Quelle A: `accounts/*/sessions.json` + `config.yml` (Device-Fallback)
      - Quelle B: `super.db` → `stats` JOIN `profile` (auf temporärer Kopie, wie sync.py)
      - Verschönerungs-Logik + Missing-Session-Logik **1:1** aus dem Altskript
      - Batch-Upload statt Einzelrequests (~30.000 → ~150 Requests) mit Einzel-Retry bei Batchfehler
      - `dry_run` für die Vorschau, `check_target()` für Verbindungstest + ALTER-SQL
- [x] `run_state.py` (neu): letzter Lauf je Upload persistiert (Zeit, Status, Zahlen)
- [x] `legacy_upload.py` (neu): plist mit `upload-*.py` finden, deaktivieren (reversibel), reaktivieren
- [x] `sync.py`: Tabellennamen aus Config, `stats` bei ausgeschaltetem Toggle überspringen, Lauf protokollieren
- [x] `scheduler.py`: ein Job je `sync_time` → Dashboard-Sync, danach Kunden-Upload
- [x] `api.py`: Config-Endpoints erweitern (Key maskiert, nur bei Eingabe überschreiben),
      neue Endpoints (`/api/stats/customer/run|preview`, `/api/stats/dashboard/run`,
      `/api/stats/test-connection`, `/api/legacy-upload/*`), Dashboard-Karte „Statistik-Uploads"
- [x] `main.py`: CLI `--customer-stats`, `--customer-stats-preview`
- [x] `.gitignore`: `run_state.json`
- [x] `CHANGELOG.md` + Tag v1.0.109
- [x] Verifikation: py_compile, API-Routen, Sessions-Parser gegen echte sessions.json,
      super.db-Mapping gegen echte stats-Daten, Dry-Run ohne Netzwerkzugriff

## Review

Alles umgesetzt und verifiziert. Neue Dateien: `customer_stats.py`, `supabase_rest.py`,
`legacy_upload.py`, `run_state.py`. Geändert: `config.py`, `config.json.example`, `sync.py`,
`scheduler.py`, `api.py`, `main.py`, `requirements.txt`, `.gitignore`, `CHANGELOG.md`.

### Verifikation
- **Zeilen-Parität**: Alle 71 echten Sessions aus einer produktiven sessions.json erzeugen
  byte-identische `users`- und `statistik`-Zeilen wie das Altskript (gleicher Random-Seed,
  Legacy-Logik im Test wortwörtlich gegenübergestellt). Auch die Missing-Session-Zeile
  stimmt inkl. Zufallswerten und session_id-Format überein.
- **super.db-Quelle**: gegen eine super.db mit dem echten Schema getestet — "13/50"→13,
  "nill"→None, dateTime "23:59:57 2026-07-15"→"2026-07-15 23:59:57", Zeilen ohne Username
  werden übersprungen, session_id bekommt den Mac-Präfix (und zwar nur einmal).
- **Upload gegen PostgREST-Mock**: Batching (1 Request statt 71), `on_conflict=session_id`
  + `merge-duplicates`, Spaltenfilterung (unbekanntes `args` wird entfernt statt Fehler),
  Dublettenschutz im zweiten Lauf (71 übersprungen, 0 neu), Missing-Einträge nur 1×/Tag,
  503-Retry mit Backoff, zeilenweiser Fallback rettet 4 von 5 Zeilen bei einer kaputten.
- **LaunchAgent-Erkennung**: findet `upload-mac17.py` und `upload_stats.py`, ignoriert
  mactool selbst, fremde Python-Skripte und Nicht-Python-Agents; Deaktivieren/Reaktivieren
  funktioniert, geschützte Agents werden abgelehnt.
- **API**: alle Routen registriert, Key nur maskiert im JSON, leeres Feld/zurückgeschickte
  Maske lassen den Key stehen, "-" löscht ihn, ungültige Datenquelle wird mit 400 abgelehnt.
- **Toggle**: `stats` wird übersprungen, `device`/`profile`/`bin` laufen weiter.
- **Scheduler**: beide Sync-Zeiten zeigen auf `run_scheduled_uploads`; ein Fehler im
  Dashboard-Sync blockiert den Kunden-Upload nicht.
- **Dashboard**: HTML/JS syntaktisch geprüft (node --check), jede `getElementById` hat ein
  Element, jeder Handler eine Funktion.
- `py_compile` über alle Dateien, CLI `--customer-stats-preview` läuft.

### Zwei Bugs beim Testen gefunden und behoben
1. Ein Tabellenname aus nur Leerzeichen hätte den gespeicherten Namen gelöscht (`if value:`
   vor dem `.strip()`).
2. plist-Pfade wurden in ein inline `onclick` interpoliert — jetzt über den Index.

### Bewusste Abweichungen vom Altskript
- Missing-Session-Einträge nur 1× pro Tag und Account statt 1× pro Lauf. Das Altskript lief
  1× nachts; bei 2 Sync-Zeiten hätte jeder inaktive Account sonst doppelt so viele
  Fake-Sessions bekommen.
- `session_id` aus super.db wird mit dem Servernamen präfigiert, weil die IDs auf allen Macs
  identisch sind. Im sessions-Modus (UUIDs) bleibt sie unverändert.
- Batch- statt Einzelrequests. Ergebnis identisch, nur ~263 statt ~31.000 Requests pro Lauf.

### Nachtrag v1.0.110 — Bug in der Spaltenerkennung

Beim ersten Test auf mac17 meldete der Verbindungstest für `users` vier fehlende Spalten
(`total_watched`, `device`, `total_scraped`, `serverzuordnung`), obwohl sie existieren.

Ursache: bei `apltfvenhqwnidmuptdp` antwortet die OpenAPI-Introspektion für den anon-Key mit
401, also griff der Fallback „eine Zeile lesen und Schlüssel zählen". Der zeigt aber nur die
Spalten, die der Key **lesen** darf — 15 von 19. Belegt ist die Existenz der Spalten dadurch,
dass das alte Skript sie mitschickt und dabei allein in der Nacht 370 Zeilen erfolgreich
geschrieben hat; PostgREST hätte sonst jede abgelehnt.

Gefährlich war nicht die Meldung, sondern dass dieselbe Erkennung den Upload-Payload
filterte: `device` und `serverzuordnung` wären beim ersten Lauf lautlos verschwunden.
Ausgelöst hat das nichts — im Screenshot stand „noch nicht gelaufen", und die 171 Zeilen von
heute ab 09:00 stammen vom alten Skript (Zeitstempel über eine Stunde im 30-Sekunden-Takt,
das Einzelrequest-Muster; mein Code schreibt in einem Batch).

Behoben: Vorfiltern nur noch bei lesbarer OpenAPI-Spec, sonst alles senden und eine Spalte
erst entfernen, wenn die Datenbank sie wirklich ablehnt (PGRST204/42703). Verbindungstest
unterscheidet jetzt `incomplete` von `unverified`. Zeilenzahl via `count=planned`, weil
`count=exact` auf der grossen users-Tabelle einen 500er auslöste.

Getestet mit einem Mock, der genau diese Konstellation nachbaut (OpenAPI 401, Stichprobe mit
15 von 19 Spalten): alle vier Felder kommen jetzt mit echten Werten an; eine tatsächlich
fehlende Spalte wird entfernt, ohne die übrigen zu verlieren.

### Nachtrag v1.0.111 — Ziel B lief auf mac17 nie

Befund am 10.08.: Ziel A (statistik) lieferte von mac17 um 08:10 einen vollstaendigen Lauf
mit 17.447 Zeilen inkl. der Session von 08:04 desselben Morgens — vorher waren es ~100
Zeilen pro Tag (Altskript, das regelmaessig frueh abbrach). Ziel B (users) dagegen hatte
von den 40 neuesten mac17-Sessions **keine einzige**; auch der manuelle Lauf am 06.08. hat
dort nichts geschrieben.

Ursache: auf mac17 war nur `customer_stats_key` hinterlegt, `customer_users_key` nicht.
Ein Ziel ohne URL/Key bekam den Status `not_configured` und wurde uebersprungen — der
Gesamtlauf meldete aber `success`, weil nur der Fall „beide Ziele unkonfiguriert" gesondert
behandelt wurde. Damit war der tote Zweig nirgends sichtbar, und schlimmer: `success` haette
das Altskript deaktiviert, obwohl es das einzige war, das users noch befuellte.

Nicht die Ursache (geprueft): die Dublettenpruefung gegen die grosse users-Tabelle
antwortet in 0,18s fuer 100 IDs.

Behoben: neuer Status `incomplete`; Auto-Deaktivierung des Altskripts nur bei echtem
`success` **und** eingeschaltetem Toggle; Dashboard zeigt je Ziel dauerhaft „Kein Key
hinterlegt" bzw. Zeitpunkt und Zeilenzahl des letzten Laufs; Ergebnis je Ziel landet in
`run_state.json`. Zusaetzlich erkennt die LaunchAgent-Suche das Skript jetzt auch in einem
Shell-Aufruf — das erklaert, warum das alte plist auf mac17 weiterlief.

### Nachtrag v1.0.112 — die tatsaechliche Ursache

Das Log von mac17 hat es beantwortet, nachdem zwei Hypothesen von mir falsch waren
(erst „Key fehlt" — der Key war da; dann „uneinheitliche Zeilen" — device/start_time sind
zu 100% gefuellt):

    HTTP 401: {"code":"42501","message":"permission denied for view users"}

`users` ist eine **View**, und der anon-Key hat darauf kein INSERT-Recht. Dazu passend
meldete derselbe Lauf `removed_columns: device, serverzuordnung, total_scraped,
total_watched` — die View stellt wirklich nur 15 Spalten bereit, es war also keine
Rechtefrage beim Lesen, sondern die View ist tatsaechlich schmaler.

Ziel A funktioniert, weil dort ein service_role-Key auf eine echte Tabelle schreibt.

Das ist ein serverseitiges Rechte-/Schema-Thema in der Kunden-Supabase, kein Mactool-Bug.
Behoben wurde die Diagnostizierbarkeit: Rechte-Fehler brechen sofort ab (1 statt 946
Requests), das Dashboard benennt den Fall samt Auswegen, und der Verbindungstest sagt
explizit, dass er nur Lesezugriff prueft.

### Nachtrag v1.0.113 — die wirkliche Ursache: falsches Schema

Die Rechteabfrage in der Kunden-Supabase zeigte zwei Relationen namens `users`:

    api.users    -> anon: SELECT                       (schmale View, 15 Spalten)
    public.users -> anon: UPDATE, DELETE, TRUNCATE, .. (echte Tabelle, 21 Spalten)

PostgREST loest eine Anfrage ohne Schema-Angabe gegen das zuerst freigegebene Schema auf —
hier `api`. `supabase-py`, das im Altskript steckt, sendet bei jedem Request
`Accept-Profile`/`Content-Profile: public` und landet deshalb auf der echten Tabelle.
Unser schlanker REST-Helper sendete diese Header nicht.

Verifiziert per Direktabfrage: ohne Header 15 Spalten, mit `Accept-Profile: public` 21 —
und die Differenz ist exakt `device, email, serverzuordnung, total_scraped, total_watched,
userid`.

Damit loest sich auch der Nachtrag zu v1.0.110 auf: die vier Spalten waren nie eine Frage
von Leserechten, sie existieren in `api.users` schlicht nicht. Der damalige Fix (nicht auf
Basis unsicherer Erkennung filtern) war trotzdem richtig und hat verhindert, dass Felder
still verloren gehen.

Behoben: beide Profile-Header werden gesendet, Schema je Ziel konfigurierbar (Default
`public`), Verbindungstest zeigt `schema.tabelle`.

### Offen (nur auf Wunsch)
- Commit + Tag v1.0.109 + Push
- Pro Mac: Keys eintragen, Toggle „Statistik (Kunde)" einschalten

---

# Integritaetspruefung der super.db vor dem Supabase-Sync (Plan)

Ziel: Eine beschaedigte super.db darf nicht hochgeladen werden. Vor dem Kopieren der DB
laeuft `PRAGMA integrity_check`. Ist alles ok, laeuft der Upload wie bisher. Ist er es
nicht, findet **kein** Upload statt und development@ebm-group.de bekommt eine Mail:
"Auf macXX war der Integrity Check der Datenbank fehlerhaft."

## Entscheidungen (mit Andreas abgestimmt)
- **Mailversand**: SMTP direkt vom Mac (keine n8n-Abhaengigkeit). Zugangsdaten pro Mac
  in der config.json bzw. ueber das Dashboard.
- **Umfang**: nur der Supabase-Sync (`sync.py`). Die Kunden-Statistik bleibt unberuehrt.

## Umsetzung
- [x] `config.py` + `config.json.example`: `db_integrity_check_enabled`, `alert_smtp_*`,
      `alert_mail_from/to` (Default development@ebm-group.de), `alert_mail_cooldown_hours`
- [x] `mailer.py` (neu): `send_alert()` per smtplib, STARTTLS/SSL/none, wirft nie
- [x] `db_integrity.py` (neu): `check_database()` (read-only, WAL-Fallback) und
      `verify_before_upload()` inkl. Mail + Wiederholsperre
- [x] `sync.py`: Gate vor `_create_temp_db()`, neuer Status `integrity_failed`, kein Upload
- [x] `api.py`: SMTP-Block in den Einstellungen, Passwort maskiert wie die Keys,
      Buttons "Testmail senden" und "Jetzt pruefen"
- [x] `tests/test_integrity.py`: echte kaputte SQLite-Datei, Mailtext, Sperre, Sync-Abbruch

## Regeln, die dabei gelten
- Fehlende DB ist **kein** Integritaetsfehler → bleibt beim bisherigen `no_db`, keine Mail
- Pruefung laeuft auf dem Original *vor* der Kopie (so wollte es Andreas), read-only
- Keine 12 Mails am Tag: eine Mail je Stoerung, danach fruehestens nach `cooldown_hours`,
  Zaehler wird bei einer erfolgreichen Pruefung zurueckgesetzt

## Review (12.08.2026)
Gebaut wie geplant, 9 von 9 Testsuiten gruen (`./tests/run_all.sh`).

**Was der Test gefunden hat:** die erste Fassung legte bei fehlender Datenbank eine neue,
leere an — `sqlite3.connect(pfad)` tut das stillschweigend, und eine leere Datenbank besteht
jede Pruefung. Der Fallback fuer WAL-Datenbanken oeffnet jetzt ueber `file:...?mode=rw`,
das legt nichts an. Zusaetzlich prueft `check_database()` vorher auf Existenz.

**Zweiter Fund:** SQLite meldet bis zu 100 Defekte in *einer* Zeile mit Zeilenumbruechen.
Ungekuerzt haette ein mehrere KB langer Block in Mail, Dashboard und run_state.json
gestanden — jetzt aufgeteilt, 8 Meldungen plus Zaehler.

**Verifiziert:**
- echte, gezielt zerstoerte SQLite-Datei wird erkannt (nicht simuliert)
- kein Upload, kein Tabellenzugriff nach fehlgeschlagener Pruefung
- Mailtext, Betreff, Empfaenger und der SMTP-Ablauf (STARTTLS vor Login, SSL ohne STARTTLS,
  ohne Benutzer kein Login, toter Server wirft nicht)
- Sperre gegen Mailflut inkl. Ruecksetzen nach erfolgreicher Pruefung
- Dashboard-Seite laedt, alle JS-IDs existieren, JS syntaktisch geprueft (`node --check`)
- SMTP-Passwort verlaesst den Server nur maskiert

**Offen:** Commit + Tag v1.0.114 + Push, danach pro Mac SMTP-Zugangsdaten eintragen und
einmal "Testmail senden" druecken. Ohne SMTP-Daten unterbleibt der Upload bei einer
kaputten DB trotzdem — die Meldung steht dann nur im Log.

---

# Fernzugriff auf die Macs (v1.0.115)

Ziel: von hier aus Fragen an einen einzelnen Mac stellen und Antworten als Daten
zurueckbekommen. Ausloeser: auf mac07 und mac22 landen keine Bot-Logs im Bucket, und der
einzige Zugang war RustDesk — Bildschirm gucken statt analysieren.

## Entscheidungen (mit Andreas abgestimmt)
- **Transport = Supabase-Briefkasten** (Polling ~15s). Tailscale scheidet aus, weil es
  bereits in einem anderen Tool haengt und dieser Weg nicht zusaetzlich angebunden werden
  kann. Cloudflare Tunnel und Reverse-SSH waeren gegangen, brauchen aber neue Software auf
  jedem Mac bzw. einen eigenen Server
- **Befehlsumfang = Diagnose + Aktionen**, kein freies Shell
- mac07 und mac22 laufen bereits als normale mactool-Installationen

## Schritte
- [x] `config.py`: `remote_control_enabled` (True), `remote_allow_actions` (True),
      `remote_poll_seconds` (15), Tabellennamen. `_mask_key` von `api.py` hierher gezogen,
      dazu `SECRET_FIELDS` + `masked_config_dict()` als einzige Ausgabestelle
- [x] `status.py`: Zustandsbericht als eine Quelle fuer Dashboard und Fernzugriff
- [x] `diagnostics.py`: Log-Upload-Kette Glied fuer Glied, plus `diagnose_database()`
- [x] `remote_commands.py`: feste Befehlsliste, duenne Wrapper um vorhandenen Code
- [x] `remote_agent.py`: holen, bedingt reservieren, Verfall, ausfuehren, zurueckschreiben,
      Heartbeat max. 1x/60s
- [x] `scheduler.py`: Poll-Job mit `max_instances=1, coalesce=True`
- [x] `supabase_rest.py`: `update()` (PATCH mit `return=representation`) ergaenzt
- [x] `tools/macctl.py` + `tools/schema_remote.sql`
- [x] `api.py`: Karte „Fernzugriff", `GET /api/remote/status`, `POST /api/diag/log-upload`
- [x] `main.py`: `--diag-upload`, `--diag-db`, `--remote-agent-once`
- [x] `tests/test_remote.py` als 10. Suite

## Review (08.09.2026)
Gebaut wie geplant, 10 von 10 Testsuiten gruen (`./tests/run_all.sh`), keine Regression in
den 9 bestehenden.

**Was beim Bauen auffiel — moeglicherweise die Ursache fuer mac07/mac22:**
Die Fixture speichert `startup_time__time_slot` als `00.00-23.59` (Punkte),
`_get_previous_timeslot()` erzeugt aber `08:00-09:59` (Doppelpunkte). Der Abgleich in
`log_uploader._get_allowed_usernames` ist ein reiner Textvergleich (`slot in time_slot`) und
kann so **nie** zutreffen. Folge: der automatische Sync laedt nie Logs hoch, nur der Knopf
„Sync Now" (`upload_all=True`) umgeht den Filter. Genau das Fehlerbild, das gemeldet wurde.
Ob die echten Macs Punkte oder Doppelpunkte speichern, ist noch **nicht** belegt — die
Diagnose beantwortet es (`steps.timeslot.configured_slots` und `hint`). Erst messen, dann
reparieren.

**Bewusste Entscheidungen:**
- `remote_control_enabled` ab Werk **an**. Sonst muesste man jeden Mac per RustDesk
  anfassen, um das Werkzeug scharfzuschalten, das RustDesk ersparen soll
- Reservieren per bedingtem PATCH ohne Retry: ein wiederholtes PATCH wuerde seine eigene,
  inzwischen falsche Bedingung erneut pruefen und eine Niederlage melden, die es nie gab
- `update` schreibt sein Ergebnis **vor** der Ausfuehrung — der Prozess beendet sich zwei
  Sekunden nach Erfolg selbst, danach kaeme kein Schreibvorgang mehr durch
- `rustdesk stop` schaltet nur den Waechter ab und beendet kein laufendes RustDesk. Der
  Fernzugriff soll nicht den letzten Notzugang kappen koennen
- Die Diagnose importiert die Filter aus `log_uploader` statt sie nachzubauen. Eine
  nachgebaute Diagnose driftet ab und behauptet irgendwann, alles sei in Ordnung

**Verifiziert:**
- Reservieren ist exklusiv (zweiter Zugriff auf dieselbe Zeile bekommt nichts)
- abgelaufener Befehl wird `expired`, der Handler wird nachweislich nicht aufgerufen
- unbekannter Name legt den Agenten nicht lahm, `poll_once` laeuft weiter
- gesperrte Aktionen: `sync` abgelehnt, `status` laeuft
- kein Geheimnis im Klartext: Supabase-Keys, SMTP-Passwort und `webhook_url` maskiert
- `logs --file "../../../etc/passwd"` bricht nicht aus dem logs-Verzeichnis aus
- Diagnose benennt bei leerer device-Tabelle die richtige Ursache und findet nach
  Einfuegen eines Phone-Geraets Account, Datei, Zeitstempel und Zielpfad

**Offen:** Tabellen anlegen (`tools/schema_remote.sql`), Zugangsdaten fuer `macctl.py`
hinterlegen, mac07 + mac22 einmalig per Dashboard auf v1.0.115 heben, dann
`macctl.py mac07,mac22 diag-upload` gegen einen funktionierenden Mac halten.
