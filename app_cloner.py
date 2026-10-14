"""App Cloner fernbedienen: eine Kopie per "Update clone" auf die Version des Originals heben.

Genau der Ablauf, den man von Hand macht (am 14.10. auf mac17/428 so belegt):

    App Cloner öffnen -> Reiter CLONED APPS -> unter "Pending updates" bei
    "Instagram • N" auf UPDATE -> Dialog "Update clone" (Safe update aus) -> UPDATE
    -> ~5 min Klonen -> "App cloned" -> INSTALL APP -> Android fragt
    "Do you want to update this app?" -> Update -> Version kontrollieren.

Schutzregeln:
- Getippt wird nur, was im aktuellen Bildschirm-Abbild eindeutig gefunden wird.
- Android-Installer: nur die Update-Frage wird bestätigt. Fragt er nach einer
  NEU-Installation, wird abgebrochen — die Kopie ist dann nicht mehr da, wie erwartet.
- Jeder unerwartete Bildschirm beendet den Vorgang mit Screenshot im Bucket.
- Der App Cloner selbst wird nie aktualisiert (Andreas).

Kopie N <-> Paket: #1 = com.instagram.androie … #8 = com.instagram.androil
(so auch in der Datenbank: noneoption__apk_cloner "APK Cloner #1" <-> androie).
"""

import logging
import re
import time

from apk_rollout import APP_CLONER, RolloutError, _adb, _center, _shell, screen

logger = logging.getLogger(__name__)

CLONE_RE = re.compile(r"^com\.instagram\.androi([e-z])$")

CLONE_TIMEOUT = 25 * 60     # Klonen dauerte 4–5 min; großzügig
INSTALL_TIMEOUT = 5 * 60
POLL = 10

# App-Cloner-Texte (Englisch belegt; Deutsch als Vorsorge, nicht belegt)
TAB_CLONED_APPS = ("CLONED APPS", "GEKLONTE APPS")
APP_CLONED = ("App cloned", "App geklont")
INSTALL_APP = ("INSTALL APP", "APP INSTALLIEREN")
INSTALL_ANYWAY = ("Install anyway", "Trotzdem installieren")
ERROR_RE = re.compile(r"\b(error|failed|fehler|fehlgeschlagen)\b", re.IGNORECASE)


def clone_number(package: str) -> int | None:
    match = CLONE_RE.match(package)
    return ord(match.group(1)) - ord("d") if match else None


def is_clone(package: str) -> bool:
    return clone_number(package) is not None


def _plain(text: str | None) -> str:
    """App Cloner streut Null-Breiten-Leerzeichen in Überschriften."""
    return (text or "").replace("​", "").strip()


def _y_range(bounds: str) -> tuple[int, int]:
    _, y1, _, y2 = map(int, re.findall(r"\d+", bounds))
    return y1, y2


class Cloner:
    """Bedient den App Cloner auf genau einem Handy."""

    def __init__(self, serial: str, progress=None):
        self.serial = serial
        self.adb = _adb()
        self.progress = progress or (lambda text: None)

    # ── Grundgriffe ───────────────────────────────────────────────────

    def elements(self) -> list[dict]:
        return screen(self.serial, picture=False)["elements"]

    def fail(self, message: str):
        try:
            picture = screen(self.serial, picture=True).get("picture")
        except Exception:
            picture = None
        raise RolloutError(message + (f" (Bildschirm: {picture})" if picture else ""))

    def tap(self, element: dict, label: str):
        x, y = _center(element["bounds"])
        _shell(self.adb, self.serial, "input", "tap", str(x), str(y))
        logger.info(f"[{self.serial}] tippe {label} @ {x},{y}")

    def key(self, code: int):
        _shell(self.adb, self.serial, "input", "keyevent", str(code))

    def find(self, elements, texts=(), ids=(), desc_prefix=None) -> list[dict]:
        hits = []
        for e in elements:
            if texts and _plain(e.get("text")) in texts:
                hits.append(e)
            elif ids and e.get("id") in ids:
                hits.append(e)
            elif desc_prefix and _plain(e.get("desc")).startswith(desc_prefix):
                hits.append(e)
        return hits

    def version_code(self, package: str) -> int | None:
        out = _shell(self.adb, self.serial, f"dumpsys package {package} | grep -m1 versionCode=")
        match = re.search(r"versionCode=(\d+)", out)
        return int(match.group(1)) if match else None

    def foreground(self) -> str:
        return _shell(self.adb, self.serial, "dumpsys window | grep -m1 mCurrentFocus")

    # ── Ablauf ────────────────────────────────────────────────────────

    def open_cloned_apps(self):
        """App Cloner vorne, Reiter CLONED APPS aktiv."""
        self.key(3)  # Home: kein halber Zustand von vorher
        time.sleep(1)
        _shell(self.adb, self.serial, "monkey", "-p", APP_CLONER, "-c", "android.intent.category.LAUNCHER", "1")
        for _ in range(6):
            time.sleep(3)
            els = self.elements()
            # "This app was built for an older version of Android" -> OK
            old_sdk = [e for e in els if e.get("id") == "android:id/message" and "older version of Android" in (e.get("text") or "")]
            ok = self.find(els, ids=("android:id/button1",))
            if old_sdk and ok:
                self.tap(ok[0], "OK (Hinweis altes Android)")
                continue
            tabs = [e for e in els if e.get("click") and any(_plain(e.get("desc")).startswith(t) for t in TAB_CLONED_APPS)]
            if tabs:
                self.tap(tabs[0], "Reiter CLONED APPS")
                time.sleep(3)
                return
        self.fail("App Cloner nicht bedienbar: Reiter CLONED APPS nicht gefunden")

    def find_update_button(self, number: int) -> dict | None:
        """UPDATE-Knopf in der Zeile "Instagram • N" unter "Pending updates"."""
        for attempt in range(4):
            els = self.elements()
            header = [e for e in els if _plain(e.get("text")).lower() in ("pending updates", "ausstehende updates")]
            if header:
                top = _y_range(header[0]["bounds"])[1]
                rows = [e for e in els if _plain(e.get("text")) == f"Instagram • {number}" and _y_range(e["bounds"])[0] >= top]
                # Die erste Zeile unter "Pending updates" mit diesem Namen; die zweite
                # wäre die Liste "All apps" weiter unten
                if rows:
                    row_top, row_bottom = _y_range(rows[0]["bounds"])
                    for e in els:
                        if _plain(e.get("text")).upper() in ("UPDATE", "AKTUALISIEREN") and e.get("click"):
                            mid = sum(_y_range(e["bounds"])) // 2
                            if row_top <= mid <= row_bottom:
                                return e
            if attempt < 3:
                _shell(self.adb, self.serial, "input", "swipe", "360", "1200", "360", "600", "400")
                time.sleep(2)
        return None

    def update_clone(self, package: str, target_code: int) -> dict:
        number = clone_number(package)
        if number is None:
            raise RolloutError(f"{package} ist keine App-Cloner-Kopie")
        before = self.version_code(package)
        if before is None:
            raise RolloutError(f"{package} ist auf {self.serial} nicht installiert")
        if before >= target_code:
            return {"package": package, "result": "schon aktuell", "version_code": before}

        self.progress(f"Kopie #{number}: App Cloner öffnen")
        self.open_cloned_apps()
        button = self.find_update_button(number)
        if button is None:
            self.fail(f"App Cloner bietet für Instagram • {number} kein Update an "
                      "(ist das Original schon auf der neuen Version?)")
        self.tap(button, f"UPDATE bei Instagram • {number}")
        time.sleep(3)

        # Dialog "Update clone"
        els = self.elements()
        if not self.find(els, texts=("Update clone", "Klon aktualisieren")):
            self.fail("Dialog 'Update clone' kam nicht")
        safe = [e for e in els if _plain(e.get("text")) == "Safe update" and e.get("checked")]
        if safe:
            self.tap(safe[0], "Safe update ausschalten")
            time.sleep(1)
            els = self.elements()
        confirm = self.find(els, ids=("android:id/button1",))
        if len(confirm) != 1:
            self.fail("Bestätigen-Knopf im Dialog 'Update clone' nicht eindeutig")
        self.tap(confirm[0], "UPDATE (klonen)")

        # Klonen abwarten
        self.progress(f"Kopie #{number}: wird gebaut (~5 min)")
        deadline = time.time() + CLONE_TIMEOUT
        while True:
            time.sleep(POLL)
            els = self.elements()
            install = self.find(els, texts=INSTALL_APP)
            if install and self.find(els, texts=APP_CLONED):
                break
            texts = " | ".join(_plain(e.get("text")) for e in els if e.get("text"))
            percent = re.search(r"\b(\d{1,3})%", texts)
            if percent:
                self.progress(f"Kopie #{number}: wird gebaut, {percent.group(1)} %")
            elif "Signing" in texts:
                self.progress(f"Kopie #{number}: wird signiert")
            elif ERROR_RE.search(texts):
                self.fail(f"App Cloner meldet einen Fehler: {texts[:300]}")
            if time.time() > deadline:
                self.fail("Klonen dauert länger als 25 Minuten")

        self.tap(install[0], "INSTALL APP")

        # Android-Installer: nur die Update-Frage bestätigen
        self.progress(f"Kopie #{number}: installieren")
        deadline = time.time() + 120
        while True:
            time.sleep(4)
            els = self.elements()
            if self.find(els, ids=("com.android.packageinstaller:id/install_confirm_question_update",)):
                update = self.find(els, ids=("com.android.packageinstaller:id/button1",))
                if len(update) != 1:
                    self.fail("Update-Knopf des Installers nicht eindeutig")
                self.tap(update[0], "Update (Android-Installer)")
                break
            if self.find(els, ids=("com.android.packageinstaller:id/install_confirm_question",)):
                cancel = self.find(els, ids=("com.android.packageinstaller:id/button2",))
                if cancel:
                    self.tap(cancel[0], "Abbrechen (wäre eine Neuinstallation)")
                self.fail("Android wollte die Kopie NEU installieren statt zu aktualisieren — abgebrochen")
            anyway = self.find(els, texts=INSTALL_ANYWAY)
            if anyway:
                self.tap(anyway[0], "Install anyway (Play Protect)")
                continue
            if time.time() > deadline:
                self.fail("Android-Installer hat nicht nach dem Update gefragt")

        # Ergebnis: Version auf dem Handy oder Fehlermeldung des Installers
        deadline = time.time() + INSTALL_TIMEOUT
        while True:
            time.sleep(POLL)
            now = self.version_code(package)
            if now is not None and now >= target_code:
                break
            els = self.elements()
            failed = [e for e in els if "install_failed" in (e.get("id") or "")]
            if failed:
                message = _plain(failed[0].get("text"))
                done = self.find(els, ids=("com.android.packageinstaller:id/button1",))
                if done:
                    self.tap(done[0], "Done")
                raise RolloutError(f"Android hat das Update abgelehnt: {message}")
            if time.time() > deadline:
                self.fail("Installation nicht abgeschlossen (5 min)")

        self.key(3)
        return {"package": package, "result": "aktualisiert", "version_code_before": before, "version_code": now}
