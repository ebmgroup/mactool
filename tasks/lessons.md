# Lessons

## Supabase `profile` enthaelt verwaiste Zeilen (2026-10-14)
- Fehler: Account-Liste direkt aus `profile` gezogen -> geloeschte Accounts (z. B.
  schoenbrunn_berlin auf mac05) tauchten als aktiv auf.
- Ursache: Der Sync loescht veraltete Zeilen nur in `bin`, nicht in `profile`.
  Stand 14.10.: 3351 von 5188 Profilzeilen gehoeren zu keinem Geraet mehr.
- Regel: Aktive Accounts immer ueber `device.profiles` bestimmen
  (`<mac>_<profil-id>`). `profile.device__name` ist kein Beleg, dass der Account lebt.
- Regel: PostgREST liefert max. 1000 Zeilen -> immer paginieren oder `count=exact` pruefen.
- Regel: Zusaetzlich alles abziehen, was in `bin` steht. Gebinnte Accounts bleiben in
  `device.profiles` gelistet (margaritt_a, mac12, seit 28.09. im bin; 22 solche Faelle).

## Erst den exakten Fehlercode holen, dann erklaeren (2026-10-14)
- Fehler: zweimal eine Ursache vermutet ("zwei Signaturen", dann "32/64 Bit"), beide falsch.
  Belegt war am Ende: DOWNGRADE beim Original, MISSING_SPLIT bei den Kopien.
- Regel: Android-Installationsfehler immer per `adb install -r` vom Mac reproduzieren — der
  liefert `Failure [INSTALL_FAILED_…]`. Die Handy-Meldung "nicht kompatibel" sagt nichts.

## Ein Lesebefehl, der still scheitert, liefert alte Daten (2026-10-14)
- Fehler: `uiautomator dump` scheiterte (Bot haelt UiAutomation), `cat` las die Datei vom
  letzten Erfolg -> stundenlang ein laengst verschwundenes "Installing…", zwei falsche
  Erklaerungen meinerseits ("Geister-Fenster").
- Regel: Ausgabedateien vor dem Erzeugen loeschen und den Erfolg pruefen. Bei Raetseln zuerst
  das Systemprotokoll (`phone-log`) lesen, bevor eine Ursache behauptet wird.

## MacTool nie mitten in einen IG-Auftrag hinein aktualisieren (2026-10-14)
- Fehler: v1.0.145 auf alle Macs verteilt, waehrend Andreas' "Mac updaten" (#7) auf mac17 lief.
  Die Abfrage hatte den offenen Auftrag gezaehlt, die Befehlskette lief trotzdem weiter.
  Der Neustart unterbrach Phone 2/20 bei 99 % (nichts kaputt, aber Lauf weg).
- Regel: Vor jedem `macctl.py … update` offene ig_update_jobs pruefen und bei > 0 HART
  abbrechen (`[ "$open" != "0" ] && exit 1`), nicht nur anzeigen. Betroffene Macs auslassen
  oder Andreas fragen.
