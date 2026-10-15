"""FastAPI web interface for mactool."""

import logging
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import (
    MASK_CHAR,
    get_config,
    mask_key as _mask_key,
    reload_config,
)
from sync import trigger_sync, check_dashboard_target
from customer_stats import (
    run_customer_stats_upload,
    check_customer_target,
    SUPERDB_UNAVAILABLE_FIELDS,
)
import db_integrity
import diagnostics
import legacy_upload
import mailer
import remote_agent
import run_state
from device_monitor import (
    get_adb_devices,
    get_devices_from_db,
    restart_adb_device,
    run_device_monitor_job,
    get_device_state,
    reset_device_reported,
    reset_all_reported,
)
from bot_manager import is_bot_running, start_bot, stop_bot, restart_bot, is_auto_restart_enabled
from rustdesk_manager import (
    is_rustdesk_running,
    start_rustdesk,
    is_watch_enabled as is_rustdesk_watch_enabled,
    set_watch_enabled as set_rustdesk_watch_enabled,
)
from updater import check_for_updates, perform_update, get_current_version, get_available_versions
from scheduler import get_scheduler
from status import build_status

logger = logging.getLogger(__name__)

app = FastAPI(title="EBM Mactool API", version="1.0.0")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic models
class ConfigUpdate(BaseModel):
    """Configuration update model.

    Key fields follow the convention: omitted/empty = keep the stored key,
    "-" = clear it. That way the dashboard can show a masked key without ever
    sending the real one back and forth.
    """

    server_name: str | None = None
    sync_times: list[str] | None = None
    blacklist: list[str] | None = None

    # Statistik (Dashboard)
    dashboard_stats_enabled: bool | None = None
    supabase_url: str | None = None
    supabase_key: str | None = None
    dashboard_table_device: str | None = None
    dashboard_table_profile: str | None = None
    dashboard_table_stats: str | None = None
    dashboard_table_bin: str | None = None

    # Statistik (Kunde)
    customer_stats_enabled: bool | None = None
    customer_stats_source: str | None = None
    customer_stats_session_limit: int | None = None
    customer_stats_url: str | None = None
    customer_stats_key: str | None = None
    customer_stats_table: str | None = None
    customer_stats_schema: str | None = None
    customer_users_url: str | None = None
    customer_users_key: str | None = None
    customer_users_table: str | None = None
    customer_users_schema: str | None = None
    auto_disable_legacy_upload: bool | None = None

    # DB-Integritätsprüfung + E-Mail-Alarm
    db_integrity_check_enabled: bool | None = None
    alert_mail_to: str | None = None
    alert_mail_from: str | None = None
    alert_smtp_host: str | None = None
    alert_smtp_port: int | None = None
    alert_smtp_user: str | None = None
    alert_smtp_password: str | None = None  # wie die Keys: maskiert rein, leer = unverändert
    bot_admin_token: str | None = None      # Admin-Token der Bot-App, wie die Keys
    alert_smtp_security: str | None = None
    alert_mail_cooldown_hours: int | None = None

    # Fernzugriff
    remote_control_enabled: bool | None = None
    remote_allow_actions: bool | None = None
    remote_poll_seconds: int | None = None


class DeviceAction(BaseModel):
    """Device action model."""

    device_id: str


class BlacklistAction(BaseModel):
    """Blacklist action model."""

    device_id: str


class TargetRequest(BaseModel):
    """Which upload target to test."""

    target: str


class AgentRequest(BaseModel):
    """Path of a legacy upload LaunchAgent."""

    path: str


def _apply_key(current: str, submitted: str | None) -> str:
    """Resolve a submitted key against the stored one."""
    if submitted is None:
        return current
    submitted = submitted.strip()
    if not submitted:
        return current  # nothing typed → unchanged
    if submitted.startswith(MASK_CHAR):
        return current  # masked placeholder sent back → unchanged
    if submitted == "-":
        return ""  # explicit clear
    return submitted


# Routes


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Get dashboard HTML."""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>EBM Mactool Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; padding: 20px; }
            .container { max-width: 1200px; margin: 0 auto; }
            h1 { color: #333; margin-bottom: 20px; }
            .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 30px; }
            .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
            .card h2 { font-size: 18px; margin-bottom: 15px; color: #333; }
            .status { display: flex; justify-content: space-between; align-items: center; margin: 10px 0; }
            .status-value { font-weight: bold; color: #0066cc; }
            .btn { background: #0066cc; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; font-size: 14px; margin-right: 10px; margin-top: 10px; }
            .btn:hover { background: #0052a3; }
            .btn:disabled { background: #ccc; cursor: not-allowed; }
            .btn-danger { background: #d32f2f; }
            .btn-danger:hover { background: #b71c1c; }
            .btn-small { padding: 5px 10px; font-size: 12px; margin-right: 5px; }
            .table { width: 100%; border-collapse: collapse; margin-top: 10px; }
            .table th, .table td { padding: 10px; text-align: left; border-bottom: 1px solid #ddd; }
            .table th { background: #f0f0f0; font-weight: bold; }
            .online { color: #4caf50; font-weight: bold; }
            .offline { color: #f44336; font-weight: bold; }
            .status-msg { padding: 10px; margin-top: 10px; border-radius: 4px; font-size: 13px; }
            .status-msg.success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
            .status-msg.error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
            .status-msg.loading { background: #e7f3ff; color: #0066cc; border: 1px solid #b3d9ff; }
            .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid #0066cc; border-top-color: transparent; border-radius: 50%; animation: spin 0.6s linear infinite; }
            @keyframes spin { to { transform: rotate(360deg); } }
            .sync-times-edit { margin-top: 10px; }
            .sync-times-edit input { padding: 8px; font-size: 14px; margin-right: 5px; border: 1px solid #ddd; border-radius: 4px; width: 80%; }
            .stats-block { border: 1px solid #e5e5e5; border-radius: 6px; padding: 15px; margin-bottom: 15px; }
            .stats-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 15px; }
            .stats-head strong { font-size: 15px; }
            .hint { font-size: 12px; color: #666; margin-top: 4px; }
            .hint.warn { color: #8a5a00; background: #fff8e6; border: 1px solid #ffe0a3; padding: 10px; border-radius: 4px; margin-top: 10px; }
            .fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 10px; margin: 12px 0 4px; }
            .fields label { display: block; font-size: 12px; color: #555; }
            .fields input, .fields select { width: 100%; padding: 7px; margin-top: 3px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; }
            .switch { display: flex; align-items: center; gap: 6px; font-size: 13px; white-space: nowrap; cursor: pointer; }
            .switch input { width: 16px; height: 16px; cursor: pointer; }
            .row-actions { margin-top: 6px; display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
            .last-run { font-size: 12px; color: #666; margin-left: 6px; }
            .code { background: #f6f6f6; border: 1px solid #e0e0e0; border-radius: 4px; padding: 10px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; white-space: pre-wrap; margin-top: 8px; max-height: 260px; overflow: auto; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 EBM Mactool Dashboard</h1>

            <div class="grid">
                <div class="card">
                    <h2>System Status</h2>
                    <div class="status">
                        <span>Bot App:</span>
                        <span class="status-value" id="bot-status">Loading...</span>
                    </div>
                    <div class="status">
                        <span>Auto-Restart:</span>
                        <span class="status-value" id="auto-restart-status">Loading...</span>
                    </div>
                    <div class="status">
                        <span>RustDesk:</span>
                        <span class="status-value" id="rustdesk-status">Loading...</span>
                    </div>
                    <div class="status">
                        <span>RustDesk Watch:</span>
                        <span class="status-value" id="rustdesk-watch-status">Loading...</span>
                    </div>
                    <div class="status">
                        <span>Statistik (Dashboard):</span>
                        <span class="status-value" id="dash-stats-status">Loading...</span>
                    </div>
                    <div class="status">
                        <span>Statistik (Kunde):</span>
                        <span class="status-value" id="cust-stats-status">Loading...</span>
                    </div>
                    <div class="status">
                        <span>Scheduler:</span>
                        <span class="status-value" id="scheduler-status">Loading...</span>
                    </div>
                    <button class="btn" id="sync-btn" onclick="triggerSync()">Sync Now</button>
                    <button class="btn" id="device-check-btn" onclick="triggerDeviceCheck()">Check Devices</button>
                    <button class="btn" id="bot-start-btn" onclick="botStart()" style="background:#4caf50;">Start Bot</button>
                    <button class="btn btn-danger" id="bot-stop-btn" onclick="botStop()">Stop Bot</button>
                    <button class="btn" id="rustdesk-start-btn" onclick="rustdeskStart()" style="background:#4caf50;">Start RustDesk</button>
                    <button class="btn btn-danger" id="rustdesk-stop-btn" onclick="rustdeskStop()">Disable RustDesk Watch</button>
                    <button class="btn" id="update-btn" onclick="triggerUpdate()" style="background:#ff9800;">Update to Latest</button>
                    <select id="version-select" style="padding:6px 10px;border-radius:4px;border:1px solid #ddd;font-size:13px;">
                        <option value="">Loading versions...</option>
                    </select>
                    <button class="btn btn-small" onclick="switchVersion()" style="background:#666;">Switch Version</button>
                    <div id="sync-status"></div>
                    <div id="device-check-status"></div>
                    <div id="bot-status-msg"></div>
                    <div id="rustdesk-status-msg"></div>
                    <div id="update-status"></div>
                </div>

                <div class="card">
                    <h2>Configuration</h2>
                    <div class="status">
                        <span>Version:</span>
                        <span class="status-value" id="app-version" style="font-size:12px;">-</span>
                    </div>
                    <div class="status">
                        <span>Server:</span>
                        <span class="status-value" id="server-name">Loading...</span>
                    </div>
                    <div class="status">
                        <span>Sync Times:</span>
                        <span class="status-value" id="sync-times">Loading...</span>
                    </div>
                    <button class="btn btn-small" onclick="editSyncTimes()">Edit</button>
                    <div id="sync-times-editor" class="sync-times-edit" style="display:none;">
                        <input type="text" id="sync-times-input" placeholder="e.g., 09:00,14:30" />
                        <button class="btn btn-small" onclick="saveSyncTimes()">Save</button>
                        <button class="btn btn-small" onclick="cancelSyncTimes()" style="background:#999;">Cancel</button>
                        <div id="sync-times-msg"></div>
                    </div>
                </div>

                <div class="card">
                    <h2>Job Schedule</h2>
                    <div id="jobs-list" style="font-size: 12px;">Loading...</div>
                </div>
            </div>

            <div class="card" style="margin-bottom:30px;">
                <h2>Statistik-Uploads</h2>

                <div class="stats-block">
                    <div class="stats-head">
                        <div>
                            <strong>Statistik (Dashboard)</strong>
                            <div class="hint">Sessiondaten aus der super.db in die EBM-Supabase. Der Schalter betrifft nur die stats-Tabelle — device, profile, bin und die Bot-Logs laufen unabhängig weiter.</div>
                        </div>
                        <label class="switch"><input type="checkbox" id="dash-enabled"> aktiv</label>
                    </div>
                    <div class="fields">
                        <label>Supabase URL<input type="text" id="dash-url" placeholder="https://xxx.supabase.co"></label>
                        <label>Key<input type="text" id="dash-key" placeholder="kein Key gespeichert"></label>
                        <label>Tabelle device<input type="text" id="dash-t-device"></label>
                        <label>Tabelle profile<input type="text" id="dash-t-profile"></label>
                        <label>Tabelle stats<input type="text" id="dash-t-stats"></label>
                        <label>Tabelle bin<input type="text" id="dash-t-bin"></label>
                    </div>
                    <div class="row-actions">
                        <button class="btn btn-small" onclick="testTarget('dashboard', 'dash-test')">Verbindung testen</button>
                        <button class="btn btn-small" style="background:#4caf50;" onclick="runStats('dashboard')">Jetzt hochladen</button>
                        <span class="last-run" id="dash-last-run"></span>
                    </div>
                    <div id="dash-test"></div>
                </div>

                <div class="stats-block">
                    <div class="stats-head">
                        <div>
                            <strong>Statistik (Kunde)</strong>
                            <div class="hint">Ersetzt das alte upload-macXX.py. Ziel A schreibt die Rohdaten (Upsert auf session_id), Ziel B die verschönerten Zahlen inkl. Missing-Session-Einträgen. Leere URL oder leerer Key = Ziel wird übersprungen. Das Schema muss stimmen — ohne Angabe nimmt PostgREST das zuerst freigegebene, was eine schmale Lese-View sein kann.</div>
                        </div>
                        <label class="switch"><input type="checkbox" id="cust-enabled"> aktiv</label>
                    </div>
                    <div class="fields">
                        <label>Datenquelle
                            <select id="cust-source" onchange="updateSourceHint()">
                                <option value="sessions">sessions.json (wie bisher)</option>
                                <option value="superdb">super.db des Macs</option>
                            </select>
                        </label>
                        <label>Sessions je Account<input type="number" id="cust-limit" min="1"></label>
                    </div>
                    <div class="hint warn" id="cust-source-hint" style="display:none;"></div>

                    <div class="fields">
                        <label>Ziel A — Supabase URL<input type="text" id="cust-stats-url" placeholder="https://xxx.supabase.co"></label>
                        <label>Ziel A — Key<input type="text" id="cust-stats-key" placeholder="kein Key gespeichert"></label>
                        <label>Ziel A — Tabelle<input type="text" id="cust-stats-table"></label>
                        <label>Ziel A — Schema<input type="text" id="cust-stats-schema" placeholder="public"></label>
                    </div>
                    <div class="row-actions">
                        <button class="btn btn-small" onclick="testTarget('customer_stats', 'cust-stats-test')">Ziel A testen</button>
                    </div>
                    <div id="cust-stats-state"></div>
                    <div id="cust-stats-test"></div>

                    <div class="fields">
                        <label>Ziel B — Supabase URL<input type="text" id="cust-users-url" placeholder="https://xxx.supabase.co"></label>
                        <label>Ziel B — Key<input type="text" id="cust-users-key" placeholder="kein Key gespeichert"></label>
                        <label>Ziel B — Tabelle<input type="text" id="cust-users-table"></label>
                        <label>Ziel B — Schema<input type="text" id="cust-users-schema" placeholder="public"></label>
                    </div>
                    <div class="row-actions">
                        <button class="btn btn-small" onclick="testTarget('customer_users', 'cust-users-test')">Ziel B testen</button>
                    </div>
                    <div id="cust-users-state"></div>
                    <div id="cust-users-test"></div>

                    <div class="row-actions">
                        <button class="btn btn-small" style="background:#4caf50;" onclick="runStats('customer')">Jetzt hochladen</button>
                        <button class="btn btn-small" style="background:#666;" onclick="previewCustomer()">Vorschau (kein Upload)</button>
                        <span class="last-run" id="cust-last-run"></span>
                    </div>
                    <div id="cust-preview"></div>
                </div>

                <div class="stats-block">
                    <div class="stats-head">
                        <div>
                            <strong>Datenbank-Prüfung + E-Mail-Alarm</strong>
                            <div class="hint">Vor jedem Supabase-Sync läuft <code>PRAGMA integrity_check</code> auf der super.db — geprüft wird das Original, bevor die Arbeitskopie gezogen wird. Ist die Datei beschädigt, findet <strong>kein</strong> Upload statt und es geht eine Mail raus. Eine fehlende Datenbank ist kein Defekt und löst keine Mail aus.</div>
                        </div>
                        <label class="switch"><input type="checkbox" id="alert-integrity"> Prüfung aktiv</label>
                    </div>
                    <div class="fields">
                        <label>Empfänger<input type="text" id="alert-to" placeholder="development@ebm-group.de"></label>
                        <label>Absender (leer = SMTP-Benutzer)<input type="text" id="alert-from" placeholder="mactool@ebm-group.de"></label>
                        <label>SMTP-Server<input type="text" id="alert-host" placeholder="smtp.example.de"></label>
                        <label>Port<input type="number" id="alert-port" min="1" max="65535"></label>
                        <label>Verschlüsselung
                            <select id="alert-security">
                                <option value="starttls">STARTTLS (Port 587)</option>
                                <option value="ssl">SSL/TLS (Port 465)</option>
                                <option value="none">keine</option>
                            </select>
                        </label>
                        <label>Benutzer<input type="text" id="alert-user" placeholder="leer = ohne Anmeldung"></label>
                        <label>Passwort<input type="password" id="alert-password" placeholder="kein Passwort gespeichert"></label>
                        <label>Wiederholsperre (Stunden)<input type="number" id="alert-cooldown" min="0"></label>
                    </div>
                    <div class="row-actions">
                        <button class="btn btn-small" onclick="sendTestMail()">Testmail senden</button>
                        <button class="btn btn-small" style="background:#666;" onclick="runIntegrityCheck()">Jetzt prüfen</button>
                        <span class="last-run" id="alert-last-check"></span>
                    </div>
                    <div id="alert-test"></div>
                </div>

                <div class="stats-block">
                    <strong>Bot-App (lokale Schnittstelle)</strong>
                    <div class="hint">Admin-Token aus der Bot-App (API Gateway → <em>Admin token</em> → „Copy“) — <strong>nicht</strong> einen der API-Keys (<code>ak_live_…</code>). Damit kann das MacTool bei „Mac updaten“ später einzelne Accounts anhalten, statt die ganze Bot-App zu stoppen. Das Token bleibt auf diesem Mac und wird nie über den Fernzugriff gelesen oder gesetzt.</div>
                    <div class="fields">
                        <label>Admin-Token<input type="password" id="bot-admin-token" placeholder="kein Token gespeichert" autocomplete="off"></label>
                    </div>
                    <div class="row-actions">
                        <button class="btn btn-small" onclick="testBotLocal()">Speichern &amp; testen</button>
                        <span class="last-run" id="bot-local-result"></span>
                    </div>
                </div>

                <div class="stats-block">
                    <strong>Altes Upload-Skript (LaunchAgent)</strong>
                    <div class="hint">Wird nach dem ersten erfolgreichen Kunden-Upload automatisch deaktiviert, damit die Zahlen nicht doppelt geschrieben werden. Deaktivieren heißt umbenennen — jederzeit reversibel.</div>
                    <label class="switch" style="margin-top:10px;"><input type="checkbox" id="cust-auto-disable"> automatisch deaktivieren</label>
                    <div id="legacy-block" style="margin-top:10px;"></div>
                </div>

                <div class="hint">Keys werden maskiert angezeigt. Feld leer lassen = Key bleibt unverändert, „-" eintragen = Key löschen.</div>
                <button class="btn" onclick="saveStatsSettings()">Einstellungen speichern</button>
                <div id="stats-settings-msg"></div>
                <div id="stats-run-msg"></div>
            </div>

            <div class="card" style="margin-bottom:30px;">
                <h2>Fernzugriff</h2>
                <div class="hint">Dieser Mac schaut regelmäßig in eine Tabelle der Dashboard-Supabase, ob dort ein Auftrag für ihn liegt, führt ihn aus und legt die Antwort daneben. So lässt sich der Mac aus der Ferne befragen, ohne einen Port zu öffnen. Ausgeführt wird nur, was auf der festen Befehlsliste steht — kein freies Shell.</div>

                <div class="stats-block">
                    <div class="stats-head">
                        <div>
                            <strong>Status</strong>
                            <div class="hint" id="remote-hint">Wird geladen…</div>
                        </div>
                        <label class="switch"><input type="checkbox" id="remote-enabled"> Fernzugriff aktiv</label>
                    </div>
                    <div class="stats-head" style="margin-top:10px;">
                        <div>
                            <strong>Eingreifende Befehle</strong>
                            <div class="hint">Aus = nur Status und Diagnose. Sync, Bot- und RustDesk-Steuerung sowie Update werden dann abgelehnt.</div>
                        </div>
                        <label class="switch"><input type="checkbox" id="remote-actions"> erlaubt</label>
                    </div>
                    <div class="row-actions" style="margin-top:12px;">
                        <button class="btn btn-small" onclick="saveRemoteSettings()">Speichern</button>
                        <button class="btn btn-small" style="background:#666;" onclick="loadRemote()">Aktualisieren</button>
                        <span class="last-run" id="remote-pending"></span>
                    </div>
                    <div id="remote-msg"></div>
                </div>

                <div class="stats-block">
                    <strong>Log-Upload analysieren</strong>
                    <div class="hint">Geht die Upload-Kette Glied für Glied durch und nennt das erste, das blockiert. Ändert nichts — es wird nichts hochgeladen und nichts gespeichert.</div>
                    <div class="row-actions" style="margin-top:10px;">
                        <button class="btn btn-small" onclick="runUploadDiagnosis()">Jetzt analysieren</button>
                    </div>
                    <div id="remote-diag"></div>
                </div>
            </div>

            <div class="card">
                <div style="display:flex;justify-content:space-between;align-items:center;">
                    <h2>Devices</h2>
                    <div>
                        <button class="btn btn-small" onclick="resetAllReported()" style="background:#ff9800;">Reset All Reported</button>
                        <button class="btn" onclick="restartAllDevices()" style="background:#e65100;">Restart All</button>
                    </div>
                </div>
                <div style="margin-bottom:10px;">
                    <strong>Blacklist:</strong> <span id="blacklist-display" style="font-size:12px;color:#666;">Loading...</span>
                </div>
                <table class="table">
                    <thead>
                        <tr>
                            <th>Serial</th>
                            <th>Name</th>
                            <th>Status</th>
                            <th>Reported</th>
                            <th>Blacklist</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="devices-tbody">
                        <tr><td colspan="6" style="text-align: center;">Loading...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>

        <script>
            let currentSyncTimes = [];

            async function loadStatus() {
                try {
                    const resp = await fetch('/api/status');
                    const data = await resp.json();

                    document.getElementById('bot-status').textContent = data.bot_running ? '✓ Running' : '✗ Stopped';
                    document.getElementById('bot-status').style.color = data.bot_running ? '#4caf50' : '#f44336';

                    const arEl = document.getElementById('auto-restart-status');
                    arEl.textContent = data.auto_restart ? '✓ Active' : '✗ Disabled';
                    arEl.style.color = data.auto_restart ? '#4caf50' : '#f44336';

                    // Show/hide start/stop buttons based on state
                    document.getElementById('bot-start-btn').style.display = data.bot_running ? 'none' : '';
                    document.getElementById('bot-stop-btn').style.display = data.bot_running ? '' : 'none';

                    // RustDesk status
                    const rdEl = document.getElementById('rustdesk-status');
                    rdEl.textContent = data.rustdesk_running ? '✓ Running' : '✗ Stopped';
                    rdEl.style.color = data.rustdesk_running ? '#4caf50' : '#f44336';

                    const rdWatchEl = document.getElementById('rustdesk-watch-status');
                    rdWatchEl.textContent = data.rustdesk_watch ? '✓ Active' : '✗ Disabled';
                    rdWatchEl.style.color = data.rustdesk_watch ? '#4caf50' : '#f44336';

                    // Show "Start RustDesk" when not running OR watch is off; show
                    // "Disable Watch" only while the watchdog is active.
                    document.getElementById('rustdesk-start-btn').style.display = (data.rustdesk_running && data.rustdesk_watch) ? 'none' : '';
                    document.getElementById('rustdesk-stop-btn').style.display = data.rustdesk_watch ? '' : 'none';

                    // Statistics uploads
                    const runs = data.last_runs || {};
                    setStatsStatus('dash-stats-status', data.dashboard_stats_enabled, runs.dashboard_stats);
                    setStatsStatus('cust-stats-status', data.customer_stats_enabled, runs.customer_stats,
                                   data.customer_stats_source);
                    renderLastRun('dash-last-run', runs.dashboard_stats);
                    renderLastRun('cust-last-run', runs.customer_stats);

                    document.getElementById('scheduler-status').textContent = '✓ Running';
                    document.getElementById('server-name').textContent = data.server_name;
                    currentSyncTimes = data.sync_times;
                    document.getElementById('sync-times').textContent = data.sync_times.join(', ');

                    // Show version info
                    if (data.version) {
                        const vEl = document.getElementById('app-version');
                        vEl.textContent = data.version;
                        vEl.style.color = '';
                        if (data.update_available && data.latest_version) {
                            vEl.textContent += ` (${data.latest_version} available)`;
                            vEl.style.color = '#ff9800';
                        }
                    }

                    if (data.jobs) {
                        let html = '';
                        for (const job of data.jobs) {
                            const nextTime = job.next_run_time ? new Date(job.next_run_time).toLocaleString() : 'N/A';
                            html += `<div style="margin-bottom: 8px;"><strong>${job.name}</strong><br/>Next: ${nextTime}</div>`;
                        }
                        document.getElementById('jobs-list').innerHTML = html;
                    }
                } catch (e) {
                    console.error('Failed to load status:', e);
                }
            }

            async function loadDevices() {
                try {
                    const resp = await fetch('/api/devices');
                    const devices = await resp.json();

                    const tbody = document.getElementById('devices-tbody');
                    if (!devices || devices.length === 0) {
                        tbody.innerHTML = '<tr><td colspan="6" style="text-align: center;">No devices</td></tr>';
                        return;
                    }

                    // Show blacklist summary
                    const blDisplay = document.getElementById('blacklist-display');
                    const blacklisted = devices.filter(d => d.blacklisted).map(d => d.serial);
                    blDisplay.textContent = blacklisted.length > 0
                        ? blacklisted.join(', ')
                        : '(none)';

                    let html = '';
                    for (const dev of devices) {
                        const status = dev.status || 'unknown';
                        const statusClass = status === 'online' ? 'online' : (status === 'unknown' ? '' : 'offline');
                        const blClass = dev.blacklisted ? 'style="opacity:0.5;"' : '';
                        const blBtn = dev.blacklisted
                            ? `<button class="btn btn-small" onclick="toggleBlacklist('${dev.serial}', false)" style="background:#4caf50;">Unblock</button>`
                            : `<button class="btn btn-small" onclick="toggleBlacklist('${dev.serial}', true)" style="background:#999;">Block</button>`;
                        const nameShort = (dev.name || '-').substring(0, 60);
                        const reportedCell = dev.reported
                            ? `<span style="color:#f44336;font-weight:bold;">Yes</span> <button class="btn btn-small" onclick="resetReported('${dev.serial}')" style="background:#ff9800;font-size:10px;">Reset</button>`
                            : `<span style="color:#999;">No</span>`;
                        html += `
                            <tr ${blClass}>
                                <td style="font-family:monospace;font-size:12px;">${dev.serial || dev.id}</td>
                                <td style="font-size:12px;">${nameShort}</td>
                                <td class="${statusClass}">${status}${dev.blacklisted ? ' (blocked)' : ''}</td>
                                <td>${reportedCell}</td>
                                <td>${blBtn}</td>
                                <td>
                                    <button class="btn btn-small" onclick="restartDevice('${dev.serial || dev.id}')">Restart</button>
                                </td>
                            </tr>
                        `;
                    }
                    tbody.innerHTML = html;
                } catch (e) {
                    console.error('Failed to load devices:', e);
                }
            }

            async function triggerSync() {
                const btn = document.getElementById('sync-btn');
                const statusDiv = document.getElementById('sync-status');

                btn.disabled = true;
                statusDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Syncing...</div>';

                try {
                    const resp = await fetch('/api/sync', { method: 'POST' });
                    const result = await resp.json();

                    if (resp.ok) {
                        let msg = '';
                        let hasError = false;
                        if (result.tables) {
                            const parts = Object.entries(result.tables).map(([k, v]) => {
                                if (v.status === 'error') {
                                    hasError = true;
                                    return `${k}=ERROR: ${v.error || 'unknown'}`;
                                }
                                return `${k}=${v.count !== undefined ? v.count : v.status}`;
                            });
                            msg = parts.join(', ');
                        }
                        if (hasError || result.status === 'partial_error') {
                            statusDiv.innerHTML = `<div class="status-msg error">Sync partial: ${msg}</div>`;
                        } else {
                            statusDiv.innerHTML = `<div class="status-msg success">Sync OK: ${msg}</div>`;
                        }
                    } else {
                        statusDiv.innerHTML = `<div class="status-msg error">Sync error: ${result.error || result.detail || 'Unknown'}</div>`;
                    }
                    loadStatus();
                } catch (e) {
                    statusDiv.innerHTML = `<div class="status-msg error">Sync failed: ${e.message}</div>`;
                } finally {
                    btn.disabled = false;
                    setTimeout(() => { statusDiv.innerHTML = ''; }, 15000);
                }
            }

            async function triggerDeviceCheck() {
                const btn = document.getElementById('device-check-btn');
                const statusDiv = document.getElementById('device-check-status');

                btn.disabled = true;
                statusDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Checking...</div>';

                try {
                    const resp = await fetch('/api/devices/check', { method: 'POST' });
                    const result = await resp.json();

                    if (resp.ok) {
                        if (result.status === 'no_devices') {
                            statusDiv.innerHTML = `<div class="status-msg success">No devices in database</div>`;
                        } else if (result.status === 'adb_not_found') {
                            statusDiv.innerHTML = `<div class="status-msg error">ADB not found. Install Android SDK or set adb_path in config.json</div>`;
                        } else if (result.status === 'error') {
                            statusDiv.innerHTML = `<div class="status-msg error">Monitor error: ${result.error || ''}</div>`;
                        } else {
                            const on = result.online || 0;
                            const off = result.offline || 0;
                            const bl = result.blacklisted || 0;
                            statusDiv.innerHTML = `<div class="status-msg success">Checked ${result.checked || 0}: online=${on}, offline=${off}, blacklisted=${bl}</div>`;
                        }
                    } else {
                        statusDiv.innerHTML = `<div class="status-msg error">Check error</div>`;
                    }
                    loadDevices();
                } catch (e) {
                    statusDiv.innerHTML = `<div class="status-msg error">Check failed: ${e.message}</div>`;
                } finally {
                    btn.disabled = false;
                    setTimeout(() => { statusDiv.innerHTML = ''; }, 15000);
                }
            }

            function editSyncTimes() {
                document.getElementById('sync-times').style.display = 'none';
                document.getElementById('sync-times-editor').style.display = 'block';
                document.getElementById('sync-times-input').value = currentSyncTimes.join(',');
            }

            function cancelSyncTimes() {
                document.getElementById('sync-times-editor').style.display = 'none';
                document.getElementById('sync-times').style.display = 'block';
                document.getElementById('sync-times-msg').innerHTML = '';
            }

            async function saveSyncTimes() {
                const input = document.getElementById('sync-times-input').value;
                const times = input.split(',').map(t => t.trim()).filter(t => t);
                const msgDiv = document.getElementById('sync-times-msg');

                if (times.length === 0) {
                    msgDiv.innerHTML = '<div class="status-msg error">✗ At least one time required</div>';
                    return;
                }

                try {
                    const resp = await fetch('/api/config', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ sync_times: times })
                    });

                    if (resp.ok) {
                        msgDiv.innerHTML = '<div class="status-msg success">✓ Saved! Service will reload...</div>';
                        setTimeout(() => {
                            loadStatus();
                            cancelSyncTimes();
                        }, 1000);
                    } else {
                        msgDiv.innerHTML = '<div class="status-msg error">✗ Save failed</div>';
                    }
                } catch (e) {
                    msgDiv.innerHTML = `<div class="status-msg error">✗ Error: ${e.message}</div>`;
                }
            }

            async function toggleBlacklist(serial, add) {
                try {
                    const method = add ? 'POST' : 'DELETE';
                    const resp = await fetch(`/api/devices/${serial}/blacklist`, { method });
                    if (resp.ok) {
                        loadDevices();
                    } else {
                        alert('Failed to update blacklist');
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                }
            }

            async function restartDevice(deviceId) {
                if (!confirm('Restart device ' + deviceId + '?')) return;
                try {
                    const resp = await fetch(`/api/devices/${deviceId}/restart`, { method: 'POST' });
                    if (resp.ok) {
                        alert('✓ Reboot command sent to ' + deviceId);
                        setTimeout(loadDevices, 5000);
                    } else {
                        const err = await resp.json().catch(() => ({}));
                        alert('✗ Restart failed: ' + (err.detail || 'unknown error'));
                    }
                } catch (e) {
                    alert('✗ Restart failed: ' + e.message);
                }
            }

            async function restartAllDevices() {
                if (!confirm('Restart ALL online devices?')) return;
                try {
                    const resp = await fetch('/api/devices/restart-all', { method: 'POST' });
                    const result = await resp.json();
                    if (result.status === 'no_devices') {
                        alert('No online devices found');
                    } else {
                        alert(`✓ Restarted ${result.restarted}/${result.total} devices`);
                        setTimeout(loadDevices, 5000);
                    }
                } catch (e) {
                    alert('✗ Restart all failed: ' + e.message);
                }
            }

            async function resetReported(serial) {
                try {
                    const resp = await fetch(`/api/devices/${serial}/reset-reported`, { method: 'POST' });
                    if (resp.ok) loadDevices();
                    else alert('Failed to reset reported status');
                } catch (e) {
                    alert('Error: ' + e.message);
                }
            }

            async function resetAllReported() {
                if (!confirm('Reset reported status for all devices? They will be re-reported if still offline.')) return;
                try {
                    const resp = await fetch('/api/devices/reset-all-reported', { method: 'POST' });
                    if (resp.ok) {
                        const result = await resp.json();
                        alert(`Reset ${result.count} device(s)`);
                        loadDevices();
                    } else {
                        alert('Failed to reset');
                    }
                } catch (e) {
                    alert('Error: ' + e.message);
                }
            }

            async function botStart() {
                const msgDiv = document.getElementById('bot-status-msg');
                msgDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Starting Bot...</div>';
                try {
                    const resp = await fetch('/api/bot/start', { method: 'POST' });
                    const result = await resp.json();
                    if (result.running) {
                        msgDiv.innerHTML = '<div class="status-msg success">Bot started. Auto-restart enabled.</div>';
                    } else {
                        msgDiv.innerHTML = '<div class="status-msg error">Bot failed to start.</div>';
                    }
                    setTimeout(loadStatus, 1000);
                } catch (e) {
                    msgDiv.innerHTML = `<div class="status-msg error">Start failed: ${e.message}</div>`;
                }
                setTimeout(() => { msgDiv.innerHTML = ''; }, 10000);
            }

            async function botStop() {
                if (!confirm('Stop Bot? Auto-restart will be disabled until you click Start again.')) return;
                const msgDiv = document.getElementById('bot-status-msg');
                msgDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Stopping Bot...</div>';
                try {
                    const resp = await fetch('/api/bot/stop', { method: 'POST' });
                    const result = await resp.json();
                    if (!result.running) {
                        msgDiv.innerHTML = '<div class="status-msg success">Bot stopped. Auto-restart disabled.</div>';
                    } else {
                        msgDiv.innerHTML = '<div class="status-msg error">Bot still running.</div>';
                    }
                    setTimeout(loadStatus, 1000);
                } catch (e) {
                    msgDiv.innerHTML = `<div class="status-msg error">Stop failed: ${e.message}</div>`;
                }
                setTimeout(() => { msgDiv.innerHTML = ''; }, 10000);
            }

            async function rustdeskStart() {
                const msgDiv = document.getElementById('rustdesk-status-msg');
                msgDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Starting RustDesk...</div>';
                try {
                    const resp = await fetch('/api/rustdesk/start', { method: 'POST' });
                    const result = await resp.json();
                    if (result.running) {
                        msgDiv.innerHTML = '<div class="status-msg success">RustDesk running. Watchdog active.</div>';
                    } else {
                        msgDiv.innerHTML = '<div class="status-msg error">RustDesk failed to start.</div>';
                    }
                    setTimeout(loadStatus, 1000);
                } catch (e) {
                    msgDiv.innerHTML = `<div class="status-msg error">Start failed: ${e.message}</div>`;
                }
                setTimeout(() => { msgDiv.innerHTML = ''; }, 10000);
            }

            async function rustdeskStop() {
                if (!confirm('Disable the RustDesk watchdog? RustDesk will NOT be closed, but it will no longer be restarted automatically if it quits.')) return;
                const msgDiv = document.getElementById('rustdesk-status-msg');
                msgDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Disabling watchdog...</div>';
                try {
                    const resp = await fetch('/api/rustdesk/stop', { method: 'POST' });
                    const result = await resp.json();
                    if (!result.watch) {
                        msgDiv.innerHTML = '<div class="status-msg success">RustDesk watchdog disabled.</div>';
                    } else {
                        msgDiv.innerHTML = '<div class="status-msg error">Failed to disable watchdog.</div>';
                    }
                    setTimeout(loadStatus, 1000);
                } catch (e) {
                    msgDiv.innerHTML = `<div class="status-msg error">Disable failed: ${e.message}</div>`;
                }
                setTimeout(() => { msgDiv.innerHTML = ''; }, 10000);
            }

            async function triggerUpdate() {
                if (!confirm('Update to latest version? Service will restart.')) return;
                const btn = document.getElementById('update-btn');
                const statusDiv = document.getElementById('update-status');

                btn.disabled = true;
                statusDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Updating...</div>';

                try {
                    const resp = await fetch('/api/update', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({})
                    });
                    const result = await resp.json();

                    if (result.status === 'success') {
                        statusDiv.innerHTML = `<div class="status-msg success">Updated to ${result.version}. Restarting...</div>`;
                        setTimeout(() => { location.reload(); }, 5000);
                    } else {
                        statusDiv.innerHTML = `<div class="status-msg error">Update failed: ${result.error || 'unknown'}</div>`;
                    }
                } catch (e) {
                    statusDiv.innerHTML = `<div class="status-msg error">Update error: ${e.message}</div>`;
                } finally {
                    btn.disabled = false;
                    setTimeout(() => { statusDiv.innerHTML = ''; }, 15000);
                }
            }

            async function switchVersion() {
                const select = document.getElementById('version-select');
                const version = select.value;
                if (!version) return;
                if (!confirm(`Switch to version ${version}? Service will restart.`)) return;

                const statusDiv = document.getElementById('update-status');
                statusDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Switching version...</div>';

                try {
                    const resp = await fetch('/api/update', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({version: version})
                    });
                    const result = await resp.json();

                    if (result.status === 'success') {
                        statusDiv.innerHTML = `<div class="status-msg success">Switched to ${result.version}. Restarting...</div>`;
                        setTimeout(() => { location.reload(); }, 5000);
                    } else {
                        statusDiv.innerHTML = `<div class="status-msg error">Switch failed: ${result.error || 'unknown'}</div>`;
                    }
                } catch (e) {
                    statusDiv.innerHTML = `<div class="status-msg error">Switch error: ${e.message}</div>`;
                }
            }

            async function loadVersions() {
                try {
                    const resp = await fetch('/api/versions');
                    const data = await resp.json();
                    const select = document.getElementById('version-select');
                    if (data.versions && data.versions.length > 0) {
                        select.innerHTML = data.versions.map(v =>
                            `<option value="${v}"${v === data.current ? ' selected' : ''}>${v}${v === data.current ? ' (current)' : ''}</option>`
                        ).join('');
                    } else {
                        select.innerHTML = '<option value="">No versions</option>';
                    }
                } catch (e) {
                    console.error('Failed to load versions:', e);
                }
            }

            // ── Statistik-Uploads ─────────────────────────────────────

            function setStatsStatus(elementId, enabled, run, source) {
                const el = document.getElementById(elementId);
                if (!el) return;
                let text = enabled ? '✓ Aktiv' : '✗ Aus';
                if (source) text += ` (${source === 'superdb' ? 'super.db' : 'sessions'})`;
                if (run && run.status && run.status !== 'success') text += ` — letzter Lauf: ${run.status}`;
                el.textContent = text;
                el.style.color = enabled ? '#4caf50' : '#f44336';
            }

            function renderLastRun(elementId, run) {
                const el = document.getElementById(elementId);
                if (!el) return;
                if (!run) { el.textContent = 'noch nicht gelaufen'; return; }
                const when = run.at ? run.at.replace('T', ' ') : '?';
                el.textContent = `zuletzt ${when} — ${run.status}${run.summary ? ': ' + run.summary : ''}`;
            }

            function setValue(id, value) {
                const el = document.getElementById(id);
                if (el) el.value = value === null || value === undefined ? '' : value;
            }

            function setKeyField(id, masked) {
                const el = document.getElementById(id);
                if (!el) return;
                el.value = '';
                el.placeholder = masked || 'kein Key gespeichert';
            }

            function updateSourceHint() {
                const select = document.getElementById('cust-source');
                const hint = document.getElementById('cust-source-hint');
                if (!select || !hint) return;
                if (select.value === 'superdb') {
                    const fields = (statsSettings && statsSettings.customer.unavailable_fields) || [];
                    hint.style.display = 'block';
                    hint.textContent = 'Hinweis: Die super.db kennt keine Session-Details. '
                        + 'Diese Felder bleiben in diesem Modus leer: ' + fields.join(', ')
                        + '. Ausserdem werden die session_id mit dem Servernamen praefigiert, '
                        + 'weil die IDs der super.db auf allen Macs gleich sind.';
                } else {
                    hint.style.display = 'none';
                }
            }

            // Permanent per-target state. A target without a key is skipped
            // silently during the upload — that must be visible at a glance,
            // not only in the message right after a run.
            function renderTargetState(elementId, label, target, outcome, lastRun) {
                const el = document.getElementById(elementId);
                if (!el) return;

                if (!target.url || !target.key_masked) {
                    const what = !target.url ? 'Keine URL' : 'Kein Key';
                    el.innerHTML = `<div class="status-msg error">${label}: ${what} hinterlegt — `
                        + `dieses Ziel wird bei jedem Upload übersprungen, es kommen dort keine Daten an.</div>`;
                    return;
                }
                if (!outcome) {
                    el.innerHTML = `<div class="hint">${label}: konfiguriert (${escapeHtml(target.table)}), noch kein Lauf protokolliert.</div>`;
                    return;
                }

                const when = lastRun && lastRun.at ? lastRun.at.replace('T', ' ') : '?';
                if (outcome.aborted === 'no_write_permission') {
                    el.innerHTML = `<div class="status-msg error">${label}: <b>keine Schreibrechte auf '${escapeHtml(outcome.table || target.table)}'</b> `
                        + `(${when}) — es wurde nichts geschrieben.<br>`
                        + `Der Key darf lesen, aber nicht schreiben. Entweder INSERT-Recht für diesen Key vergeben, `
                        + `einen Key mit Schreibrecht (service_role) eintragen, oder als Ziel die echte Tabelle `
                        + `statt einer View angeben.</div>`
                        + `<div class="code">${escapeHtml((outcome.errors || [])[0] || '')}</div>`;
                    if (outcome.removed_columns && outcome.removed_columns.length) {
                        el.innerHTML += `<div class="hint warn">Ausserdem kennt '${escapeHtml(outcome.table || target.table)}' diese Spalten nicht, `
                            + `sie wurden weggelassen: ${escapeHtml(outcome.removed_columns.join(', '))}</div>`;
                    }
                    return;
                }
                if (outcome.status === 'not_configured') {
                    el.innerHTML = `<div class="status-msg error">${label}: wurde beim letzten Lauf (${when}) übersprungen — URL oder Key fehlten.</div>`;
                } else if (outcome.status === 'error') {
                    el.innerHTML = `<div class="status-msg error">${label}: letzter Lauf ${when} fehlgeschlagen — ${escapeHtml(outcome.error || '')}</div>`;
                } else {
                    const extra = outcome.skipped_existing !== undefined
                        ? `, ${outcome.skipped_existing} bereits vorhanden` : '';
                    const failed = outcome.failed ? `, ${outcome.failed} fehlgeschlagen` : '';
                    const cls = (outcome.status === 'success' && !outcome.failed) ? 'success' : 'error';
                    el.innerHTML = `<div class="status-msg ${cls}">${label}: ${when} — `
                        + `${outcome.written} Zeilen geschrieben${extra}${failed}</div>`;
                }
            }

            let statsSettings = null;

            async function loadStatsSettings() {
                try {
                    const resp = await fetch('/api/stats/settings');
                    const data = await resp.json();
                    statsSettings = data;

                    document.getElementById('dash-enabled').checked = !!data.dashboard.enabled;
                    setValue('dash-url', data.dashboard.url);
                    setKeyField('dash-key', data.dashboard.key_masked);
                    setValue('dash-t-device', data.dashboard.tables.device);
                    setValue('dash-t-profile', data.dashboard.tables.profile);
                    setValue('dash-t-stats', data.dashboard.tables.stats);
                    setValue('dash-t-bin', data.dashboard.tables.bin);

                    document.getElementById('cust-enabled').checked = !!data.customer.enabled;
                    setValue('cust-source', data.customer.source || 'sessions');
                    setValue('cust-limit', data.customer.session_limit);
                    setValue('cust-stats-url', data.customer.stats.url);
                    setKeyField('cust-stats-key', data.customer.stats.key_masked);
                    setValue('cust-stats-table', data.customer.stats.table);
                    setValue('cust-stats-schema', data.customer.stats.schema);
                    setValue('cust-users-url', data.customer.users.url);
                    setKeyField('cust-users-key', data.customer.users.key_masked);
                    setValue('cust-users-table', data.customer.users.table);
                    setValue('cust-users-schema', data.customer.users.schema);
                    document.getElementById('cust-auto-disable').checked = !!data.customer.auto_disable_legacy_upload;

                    const alerts = data.alerts || {};
                    document.getElementById('alert-integrity').checked = !!alerts.integrity_check_enabled;
                    setValue('alert-to', alerts.mail_to);
                    setValue('alert-from', alerts.mail_from);
                    setValue('alert-host', alerts.smtp_host);
                    setValue('alert-port', alerts.smtp_port);
                    setValue('alert-security', alerts.smtp_security || 'starttls');
                    setValue('alert-user', alerts.smtp_user);
                    setKeyField('alert-password', alerts.password_masked);
                    document.getElementById('alert-password').placeholder =
                        alerts.password_masked || 'kein Passwort gespeichert';
                    setValue('alert-cooldown', alerts.cooldown_hours);
                    const bot = data.bot || {};
                    setKeyField('bot-admin-token', bot.admin_token_masked);
                    document.getElementById('bot-admin-token').placeholder =
                        bot.admin_token_masked || 'kein Token gespeichert';
                    renderIntegrityRun((data.last_runs || {}).db_integrity, alerts.db_path);

                    const lastRun = (data.last_runs || {}).customer_stats;
                    renderTargetState('cust-stats-state', 'Ziel A', data.customer.stats,
                                      (lastRun && lastRun.detail || {}).statistik, lastRun);
                    renderTargetState('cust-users-state', 'Ziel B', data.customer.users,
                                      (lastRun && lastRun.detail || {}).users, lastRun);

                    updateSourceHint();
                    renderLegacy(data.legacy_upload);
                } catch (e) {
                    console.error('Failed to load stats settings:', e);
                }
            }

            async function saveStatsSettings() {
                const msgDiv = document.getElementById('stats-settings-msg');
                const body = {
                    dashboard_stats_enabled: document.getElementById('dash-enabled').checked,
                    supabase_url: document.getElementById('dash-url').value.trim(),
                    dashboard_table_device: document.getElementById('dash-t-device').value.trim(),
                    dashboard_table_profile: document.getElementById('dash-t-profile').value.trim(),
                    dashboard_table_stats: document.getElementById('dash-t-stats').value.trim(),
                    dashboard_table_bin: document.getElementById('dash-t-bin').value.trim(),
                    customer_stats_enabled: document.getElementById('cust-enabled').checked,
                    customer_stats_source: document.getElementById('cust-source').value,
                    customer_stats_session_limit: parseInt(document.getElementById('cust-limit').value, 10) || 90,
                    customer_stats_url: document.getElementById('cust-stats-url').value.trim(),
                    customer_stats_table: document.getElementById('cust-stats-table').value.trim(),
                    customer_stats_schema: document.getElementById('cust-stats-schema').value.trim(),
                    customer_users_url: document.getElementById('cust-users-url').value.trim(),
                    customer_users_table: document.getElementById('cust-users-table').value.trim(),
                    customer_users_schema: document.getElementById('cust-users-schema').value.trim(),
                    auto_disable_legacy_upload: document.getElementById('cust-auto-disable').checked,
                    db_integrity_check_enabled: document.getElementById('alert-integrity').checked,
                    alert_mail_to: document.getElementById('alert-to').value.trim(),
                    alert_mail_from: document.getElementById('alert-from').value.trim(),
                    alert_smtp_host: document.getElementById('alert-host').value.trim(),
                    alert_smtp_port: parseInt(document.getElementById('alert-port').value, 10) || 587,
                    alert_smtp_user: document.getElementById('alert-user').value.trim(),
                    alert_smtp_security: document.getElementById('alert-security').value,
                    alert_mail_cooldown_hours: parseInt(document.getElementById('alert-cooldown').value, 10) || 0
                };

                // Keys only travel when something was actually typed
                const keyFields = {
                    supabase_key: 'dash-key',
                    customer_stats_key: 'cust-stats-key',
                    customer_users_key: 'cust-users-key',
                    alert_smtp_password: 'alert-password',
                    bot_admin_token: 'bot-admin-token'
                };
                for (const [field, id] of Object.entries(keyFields)) {
                    const value = document.getElementById(id).value.trim();
                    if (value) body[field] = value;
                }

                msgDiv.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Speichern...</div>';
                try {
                    const resp = await fetch('/api/config', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(body)
                    });
                    if (resp.ok) {
                        msgDiv.innerHTML = '<div class="status-msg success">✓ Gespeichert</div>';
                        loadStatsSettings();
                        loadStatus();
                    } else {
                        const err = await resp.json().catch(() => ({}));
                        msgDiv.innerHTML = `<div class="status-msg error">✗ Speichern fehlgeschlagen: ${err.detail || resp.status}</div>`;
                    }
                } catch (e) {
                    msgDiv.innerHTML = `<div class="status-msg error">✗ Fehler: ${e.message}</div>`;
                }
                setTimeout(() => { msgDiv.innerHTML = ''; }, 10000);
            }

            function renderIntegrityRun(run, dbPath) {
                const el = document.getElementById('alert-last-check');
                if (!el) return;
                if (!run) {
                    el.textContent = dbPath ? `noch nicht geprüft — ${dbPath}` : 'noch nicht geprüft';
                    el.style.color = '';
                    return;
                }
                const when = run.at ? run.at.replace('T', ' ') : '?';
                el.textContent = `zuletzt ${when} — ${run.summary || run.status}`;
                el.style.color = run.status === 'success' ? '' : '#f44336';
            }

            async function testBotLocal() {
                const out = document.getElementById('bot-local-result');
                const field = document.getElementById('bot-admin-token');
                const typed = field.value.trim();
                out.style.color = '';
                try {
                    // Eingetipptes Token zuerst speichern (Maske = nichts Neues eingetippt)
                    if (typed && !typed.startsWith('\u2022')) {
                        out.textContent = 'speichere…';
                        const saved = await fetch('/api/config', {
                            method: 'PUT',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ bot_admin_token: typed })
                        });
                        if (!saved.ok) throw new Error('Speichern fehlgeschlagen (HTTP ' + saved.status + ')');
                        field.value = '';
                        field.placeholder = '••••••••' + typed.slice(-4);
                    }
                    out.textContent = 'prüfe…';
                    const resp = await fetch('/api/bot/local-test', { method: 'POST' });
                    const data = await resp.json();
                    out.textContent = (data.ok ? '✓ ' : '✗ ') + (data.message || '');
                    out.style.color = data.ok ? '#2e7d32' : '#c62828';
                } catch (e) {
                    out.textContent = '✗ ' + e;
                    out.style.color = '#c62828';
                }
            }

            async function sendTestMail() {
                const out = document.getElementById('alert-test');
                out.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Sende Testmail...</div>';
                try {
                    const resp = await fetch('/api/alert/test', { method: 'POST' });
                    const data = await resp.json();
                    out.innerHTML = resp.ok
                        ? `<div class="status-msg success">✓ Testmail an ${data.to} verschickt</div>`
                        : `<div class="status-msg error">✗ ${data.detail || 'Versand fehlgeschlagen'}</div>`;
                } catch (e) {
                    out.innerHTML = `<div class="status-msg error">✗ Fehler: ${e.message}</div>`;
                }
            }

            async function runIntegrityCheck() {
                const out = document.getElementById('alert-test');
                out.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Prüfe Datenbank...</div>';
                try {
                    const resp = await fetch('/api/db/integrity-check', { method: 'POST' });
                    const data = await resp.json();
                    if (!resp.ok) {
                        out.innerHTML = `<div class="status-msg error">✗ ${data.detail || 'Prüfung fehlgeschlagen'}</div>`;
                        return;
                    }
                    // Bewusst kein loadStatsSettings(): die Handprüfung schreibt keinen
                    // Zustand und würde nur ungespeicherte Eingaben im Formular verwerfen.
                    out.innerHTML = data.ok
                        ? `<div class="status-msg success">✓ Datenbank in Ordnung (${data.seconds}s)</div>`
                        : `<div class="status-msg error">✗ Datenbank beschädigt: ${data.detail}</div>`;
                } catch (e) {
                    out.innerHTML = `<div class="status-msg error">✗ Fehler: ${e.message}</div>`;
                }
            }

            async function loadRemote() {
                const hint = document.getElementById('remote-hint');
                const pending = document.getElementById('remote-pending');
                try {
                    const resp = await fetch('/api/remote/status');
                    const data = await resp.json();

                    document.getElementById('remote-enabled').checked = !!data.enabled;
                    document.getElementById('remote-actions').checked = !!data.actions_allowed;

                    if (!data.configured) {
                        hint.innerHTML = '<strong>Kein Supabase-Key hinterlegt</strong> — ohne den gibt es keinen Briefkasten und keinen Fernzugriff.';
                    } else if (!data.enabled) {
                        hint.textContent = 'Ausgeschaltet — dieser Mac holt keine Aufträge ab.';
                    } else if (data.reachable === false) {
                        hint.innerHTML = `<strong>Tabelle nicht erreichbar:</strong> ${data.error || 'unbekannter Fehler'}. Ist tools/schema_remote.sql eingespielt?`;
                    } else {
                        hint.textContent = `Aktiv — schaut alle ${data.poll_seconds}s in „${data.commands_table}“. `
                            + `${(data.commands || []).length} Befehle verfügbar.`;
                    }

                    const open = data.pending || [];
                    pending.textContent = open.length
                        ? `${open.length} offen: ${open.map(c => c.command + ' (' + c.status + ')').join(', ')}`
                        : (data.reachable ? 'nichts offen' : '');
                } catch (e) {
                    hint.textContent = `Status nicht abrufbar: ${e.message}`;
                }
            }

            async function saveRemoteSettings() {
                const out = document.getElementById('remote-msg');
                out.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Speichere...</div>';
                try {
                    const resp = await fetch('/api/config', {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            remote_control_enabled: document.getElementById('remote-enabled').checked,
                            remote_allow_actions: document.getElementById('remote-actions').checked
                        })
                    });
                    const data = await resp.json();
                    out.innerHTML = resp.ok
                        ? '<div class="status-msg success">✓ Gespeichert. Ein Ein-/Ausschalten wirkt erst nach einem Neustart des Dienstes.</div>'
                        : `<div class="status-msg error">✗ ${data.detail || 'Speichern fehlgeschlagen'}</div>`;
                    if (resp.ok) loadRemote();
                } catch (e) {
                    out.innerHTML = `<div class="status-msg error">✗ Fehler: ${e.message}</div>`;
                }
            }

            async function runUploadDiagnosis() {
                const out = document.getElementById('remote-diag');
                out.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Analysiere Upload-Kette...</div>';
                try {
                    const resp = await fetch('/api/diag/log-upload', { method: 'POST' });
                    const data = await resp.json();
                    if (!resp.ok) {
                        out.innerHTML = `<div class="status-msg error">✗ ${data.detail || 'Analyse fehlgeschlagen'}</div>`;
                        return;
                    }
                    const cls = (data.blocking || []).length ? 'error' : 'success';
                    out.innerHTML = `<div class="status-msg ${cls}">${data.verdict}</div>`
                        + '<pre style="font-size:11px;max-height:340px;overflow:auto;background:#f5f5f5;padding:10px;border-radius:4px;">'
                        + JSON.stringify(data.steps, null, 2).replace(/</g, '&lt;')
                        + '</pre>';
                } catch (e) {
                    out.innerHTML = `<div class="status-msg error">✗ Fehler: ${e.message}</div>`;
                }
            }

            async function testTarget(target, outputId) {
                const out = document.getElementById(outputId);
                out.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Teste Verbindung...</div>';
                try {
                    const resp = await fetch('/api/stats/test', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ target: target })
                    });
                    const data = await resp.json();
                    if (!resp.ok) {
                        out.innerHTML = `<div class="status-msg error">${data.detail || 'Test fehlgeschlagen'}</div>`;
                        return;
                    }
                    out.innerHTML = data.targets ? data.targets.map(renderTargetInfo).join('') : renderTargetInfo(data);
                } catch (e) {
                    out.innerHTML = `<div class="status-msg error">Test fehlgeschlagen: ${e.message}</div>`;
                }
            }

            function renderTargetInfo(info) {
                const label = info.source_table ? `${info.source_table} → ${info.table}`
                    : (info.schema ? `${info.schema}.${info.table}` : info.table);
                if (info.status === 'not_configured') {
                    return `<div class="status-msg error">${label}: nicht konfiguriert (${info.error || ''})</div>`;
                }
                if (info.status === 'error') {
                    return `<div class="status-msg error">${label}: ${info.error || 'Fehler'}</div>`;
                }
                let html = '';
                const rows = info.rows === null || info.rows === undefined ? '?' : '~' + info.rows;
                const note = info.note ? ` — ${info.note}` : '';
                if (info.status === 'incomplete') {
                    html += `<div class="status-msg error">${label}: erreichbar, aber ${info.missing.length} Spalte(n) fehlen wirklich (${info.columns} vorhanden, ${rows} Zeilen)${note}</div>`;
                    html += `<div class="code">${escapeHtml(info.missing.join(', '))}\n\n${escapeHtml(info.sql || '')}</div>`;
                } else if (info.status === 'unverified') {
                    html += `<div class="status-msg loading">${label}: erreichbar (${info.columns} lesbare Spalten, ${rows} Zeilen)${note}<br>`
                        + `Der Key darf die Tabellenstruktur nicht auslesen. ${info.missing.length} Spalte(n) waren in der Stichprobe nicht sichtbar — `
                        + `das kann heissen, dass sie fehlen, oder nur, dass der Key sie nicht lesen darf. `
                        + `<b>Beim Upload werden sie trotzdem mitgeschickt</b> und nur weggelassen, wenn die Datenbank sie wirklich ablehnt.</div>`;
                    html += `<div class="code">Nicht sichtbar: ${escapeHtml(info.missing.join(', '))}\n\nNur falls sie tatsaechlich fehlen:\n${escapeHtml(info.sql || '')}</div>`;
                } else {
                    html += `<div class="status-msg success">${label}: OK — ${info.columns} Spalten, ${rows} Zeilen${note}</div>`;
                }
                html += `<div class="hint">Der Test prüft nur den Lesezugriff — ob der Key auch schreiben darf, `
                    + `zeigt sich erst beim Upload (siehe Zeile darüber nach einem Lauf).</div>`;
                if (info.hint) html += `<div class="code">${escapeHtml(info.hint)}</div>`;
                return html;
            }

            function escapeHtml(text) {
                return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
            }

            async function runStats(which) {
                const label = which === 'dashboard' ? 'Statistik (Dashboard)' : 'Statistik (Kunde)';
                const msgDiv = document.getElementById('stats-run-msg');
                const url = which === 'dashboard' ? '/api/stats/dashboard/run' : '/api/stats/customer/run';

                msgDiv.innerHTML = `<div class="status-msg loading"><span class="spinner"></span> ${label} läuft... (kann einige Minuten dauern)</div>`;
                try {
                    const resp = await fetch(url, { method: 'POST' });
                    const result = await resp.json();
                    if (!resp.ok) {
                        msgDiv.innerHTML = `<div class="status-msg error">${label}: ${result.detail || 'Fehler'}</div>`;
                        return;
                    }
                    const summary = which === 'dashboard'
                        ? Object.entries(result.tables || {}).map(([k, v]) => `${k}=${v.count !== undefined ? v.count : v.status}`).join(', ')
                        : `${result.sessions} Sessions | ${result.statistik.table || 'Ziel A'}: ${result.statistik.written !== undefined ? result.statistik.written : result.statistik.status}`
                          + ` | ${result.users.table || 'Ziel B'}: ${result.users.written !== undefined ? result.users.written + ' neu, ' + result.users.skipped_existing + ' vorhanden' : result.users.status}`;
                    const cssClass = (result.status === 'success') ? 'success' : 'error';
                    msgDiv.innerHTML = `<div class="status-msg ${cssClass}">${label} ${result.status}: ${summary}${result.error ? ' — ' + result.error : ''}</div>`;
                    loadStatus();
                    loadStatsSettings();
                } catch (e) {
                    msgDiv.innerHTML = `<div class="status-msg error">${label} fehlgeschlagen: ${e.message}</div>`;
                }
                setTimeout(() => { msgDiv.innerHTML = ''; }, 30000);
            }

            async function previewCustomer() {
                const out = document.getElementById('cust-preview');
                out.innerHTML = '<div class="status-msg loading"><span class="spinner"></span> Daten werden eingelesen...</div>';
                try {
                    const resp = await fetch('/api/stats/customer/preview', { method: 'POST' });
                    const data = await resp.json();
                    if (!resp.ok || data.status === 'error') {
                        out.innerHTML = `<div class="status-msg error">Vorschau fehlgeschlagen: ${data.error || data.detail || 'unbekannt'}</div>`;
                        return;
                    }
                    out.innerHTML = `<div class="status-msg success">Quelle ${data.source}: ${data.accounts} Accounts, `
                        + `${data.sessions} Sessions, ${data.missing_sessions} Missing-Einträge — nichts hochgeladen.</div>`
                        + `<div class="code">${escapeHtml(JSON.stringify(data.preview, null, 1))}</div>`;
                } catch (e) {
                    out.innerHTML = `<div class="status-msg error">Vorschau fehlgeschlagen: ${e.message}</div>`;
                }
            }

            function renderLegacy(info) {
                const el = document.getElementById('legacy-block');
                if (!el) return;
                if (!info || !info.found) {
                    el.innerHTML = '<div class="hint">Kein LaunchAgent gefunden, der ein Upload-Skript startet.</div>';
                    return;
                }
                // Index instead of the path: file paths must never end up inside
                // an inline onclick attribute.
                el.innerHTML = info.agents.map((agent, index) => `
                    <div class="status">
                        <span>${escapeHtml(agent.label)}<div class="hint">${escapeHtml(agent.script)}</div></span>
                        <span>
                            <span class="${agent.enabled ? 'online' : 'offline'}">${agent.enabled ? 'aktiv' : 'deaktiviert'}</span>
                            <button class="btn btn-small" style="background:${agent.enabled ? '#d32f2f' : '#666'};"
                                onclick="legacyAction('${agent.enabled ? 'disable' : 'enable'}', ${index})">
                                ${agent.enabled ? 'Deaktivieren' : 'Reaktivieren'}
                            </button>
                        </span>
                    </div>`).join('');
            }

            async function legacyAction(action, index) {
                const agent = statsSettings && statsSettings.legacy_upload
                    && statsSettings.legacy_upload.agents[index];
                if (!agent) return;
                const path = agent.path;
                if (action === 'disable' && !confirm('Diesen LaunchAgent deaktivieren? Die Datei wird nur umbenannt und kann jederzeit reaktiviert werden.')) return;
                try {
                    const resp = await fetch(`/api/legacy-upload/${action}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path: path })
                    });
                    if (!resp.ok) {
                        const err = await resp.json().catch(() => ({}));
                        alert('Fehlgeschlagen: ' + (err.detail || resp.status));
                    }
                    loadStatsSettings();
                } catch (e) {
                    alert('Fehler: ' + e.message);
                }
            }

            // Load data on page load and refresh every 30s
            loadStatus();
            loadDevices();
            loadVersions();
            loadStatsSettings();
            loadRemote();
            setInterval(loadStatus, 30000);
            setInterval(loadDevices, 30000);
            setInterval(loadRemote, 30000);
        </script>
    </body>
    </html>
    """


@app.get("/api/status")
async def get_status():
    """Get system status."""
    try:
        return build_status(check_updates=True)
    except Exception as e:
        logger.error(f"Failed to get status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync")
async def sync_now():
    """Trigger sync immediately.

    Manual trigger from the dashboard — uploads ALL Phone logs (upload_all_logs=True),
    not just the previous 2h timeslot that auto-syncs use.
    """
    try:
        result = trigger_sync(upload_all_logs=True)
        return result
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/devices")
async def get_devices():
    """Get all devices: merge DB profiles + live ADB connections."""
    try:
        config = get_config()
        blacklist = set(config.blacklist)
        db_devices = get_devices_from_db()
        device_state = get_device_state()
        adb_online = get_adb_devices()  # set of serials currently connected

        # Index DB devices by serial
        seen_serials: set[str] = set()
        result = []
        for device in db_devices:
            serial = device["serial"]
            seen_serials.add(serial)

            if serial in adb_online:
                device["status"] = "online"
                device["reported"] = False
            else:
                state_entry = device_state.get(serial) or device_state.get(device["id"])
                if isinstance(state_entry, dict):
                    device["status"] = state_entry.get("status", "unknown")
                    device["reported"] = state_entry.get("reported", False)
                elif isinstance(state_entry, str):
                    device["status"] = state_entry
                    device["reported"] = state_entry == "offline"
                else:
                    device["status"] = "unknown"
                    device["reported"] = False

            device["blacklisted"] = serial in blacklist or device["id"] in blacklist
            result.append(device)

        # Add ADB devices not in DB
        for serial in sorted(adb_online):
            if serial not in seen_serials:
                result.append({
                    "id": serial,
                    "name": "(not in database)",
                    "serial": serial,
                    "status": "online",
                    "reported": False,
                    "blacklisted": serial in blacklist,
                })

        return result
    except Exception as e:
        logger.error(f"Failed to get devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/check")
async def check_devices():
    """Check all devices."""
    try:
        result = run_device_monitor_job()
        return result
    except Exception as e:
        logger.error(f"Device check failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/restart-all")
async def restart_all_devices():
    """Restart all online ADB devices."""
    import asyncio
    try:
        online = await asyncio.to_thread(get_adb_devices)
        if not online:
            return {"status": "no_devices", "restarted": 0}

        results = {}
        for serial in sorted(online):
            success = await asyncio.to_thread(restart_adb_device, serial)
            results[serial] = "ok" if success else "failed"

        ok_count = sum(1 for v in results.values() if v == "ok")
        return {"status": "done", "restarted": ok_count, "total": len(online), "details": results}
    except Exception as e:
        logger.error(f"Restart all devices failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/reset-all-reported")
async def reset_all_reported_endpoint():
    """Reset reported flag for all devices."""
    count = reset_all_reported()
    return {"status": "reset", "count": count}


@app.post("/api/devices/{device_id}/reset-reported")
async def reset_device_reported_endpoint(device_id: str):
    """Reset reported flag for a single device."""
    success = reset_device_reported(device_id)
    if success:
        return {"status": "reset", "device_id": device_id}
    raise HTTPException(status_code=404, detail="Device not found in state cache")


@app.post("/api/devices/{device_id}/restart")
async def restart_device(device_id: str):
    """Restart a specific device via ADB reboot."""
    import asyncio
    try:
        success = await asyncio.to_thread(restart_adb_device, device_id)
        if success:
            return {"status": "restart_initiated", "device_id": device_id}
        else:
            raise HTTPException(status_code=500, detail="ADB reboot failed")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Device restart failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/devices/{device_id}/blacklist")
async def add_to_blacklist(device_id: str):
    """Add device to blacklist."""
    try:
        config = get_config()
        if device_id not in config.blacklist:
            config.blacklist.append(device_id)
            config.save()
            logger.info(f"Added {device_id} to blacklist")

        return {"status": "added", "device_id": device_id, "blacklist": config.blacklist}
    except Exception as e:
        logger.error(f"Blacklist operation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/devices/{device_id}/blacklist")
async def remove_from_blacklist(device_id: str):
    """Remove device from blacklist."""
    try:
        config = get_config()
        if device_id in config.blacklist:
            config.blacklist.remove(device_id)
            config.save()
            logger.info(f"Removed {device_id} from blacklist")

        return {"status": "removed", "device_id": device_id, "blacklist": config.blacklist}
    except Exception as e:
        logger.error(f"Blacklist operation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config")
async def get_config_endpoint():
    """Get current configuration."""
    try:
        config = get_config()
        return {
            "server_name": config.server_name,
            "sync_times": config.sync_times,
            "blacklist": config.blacklist,
            "bot_app_path": config.bot_app_path,
        }
    except Exception as e:
        logger.error(f"Failed to get config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/config")
async def update_config(update: ConfigUpdate):
    """Update configuration."""
    try:
        config = get_config()

        if update.server_name is not None:
            config.server_name = update.server_name
        if update.sync_times is not None:
            config.sync_times = update.sync_times
        if update.blacklist is not None:
            config.blacklist = update.blacklist

        # ── Statistik (Dashboard) ──
        if update.dashboard_stats_enabled is not None:
            config.dashboard_stats_enabled = update.dashboard_stats_enabled
        if update.supabase_url is not None:
            config.supabase_url = update.supabase_url.strip()
        config.supabase_key = _apply_key(config.supabase_key, update.supabase_key)
        for field in ("device", "profile", "stats", "bin"):
            # Strip first: a whitespace-only name would otherwise wipe the table
            value = (getattr(update, f"dashboard_table_{field}") or "").strip()
            if value:
                setattr(config, f"dashboard_table_{field}", value)

        # ── Statistik (Kunde) ──
        if update.customer_stats_enabled is not None:
            config.customer_stats_enabled = update.customer_stats_enabled
        if update.customer_stats_source is not None:
            source = update.customer_stats_source.strip()
            if source not in ("sessions", "superdb"):
                raise HTTPException(
                    status_code=400,
                    detail="customer_stats_source muss 'sessions' oder 'superdb' sein",
                )
            config.customer_stats_source = source
        if update.customer_stats_session_limit is not None:
            config.customer_stats_session_limit = max(
                1, int(update.customer_stats_session_limit)
            )
        if update.customer_stats_url is not None:
            config.customer_stats_url = update.customer_stats_url.strip()
        config.customer_stats_key = _apply_key(
            config.customer_stats_key, update.customer_stats_key
        )
        if (update.customer_stats_table or "").strip():
            config.customer_stats_table = update.customer_stats_table.strip()
        if (update.customer_stats_schema or "").strip():
            config.customer_stats_schema = update.customer_stats_schema.strip()
        if update.customer_users_url is not None:
            config.customer_users_url = update.customer_users_url.strip()
        config.customer_users_key = _apply_key(
            config.customer_users_key, update.customer_users_key
        )
        if (update.customer_users_table or "").strip():
            config.customer_users_table = update.customer_users_table.strip()
        if (update.customer_users_schema or "").strip():
            config.customer_users_schema = update.customer_users_schema.strip()
        if update.auto_disable_legacy_upload is not None:
            config.auto_disable_legacy_upload = update.auto_disable_legacy_upload

        # ── DB-Integritätsprüfung + E-Mail-Alarm ──
        if update.db_integrity_check_enabled is not None:
            config.db_integrity_check_enabled = update.db_integrity_check_enabled
        if update.alert_mail_to is not None:
            config.alert_mail_to = update.alert_mail_to.strip()
        if update.alert_mail_from is not None:
            config.alert_mail_from = update.alert_mail_from.strip()
        if update.alert_smtp_host is not None:
            config.alert_smtp_host = update.alert_smtp_host.strip()
        if update.alert_smtp_port is not None:
            port = int(update.alert_smtp_port)
            if not 1 <= port <= 65535:
                raise HTTPException(status_code=400, detail="alert_smtp_port muss 1-65535 sein")
            config.alert_smtp_port = port
        if update.alert_smtp_user is not None:
            config.alert_smtp_user = update.alert_smtp_user.strip()
        config.alert_smtp_password = _apply_key(
            config.alert_smtp_password, update.alert_smtp_password
        )
        config.bot_admin_token = _apply_key(config.bot_admin_token, update.bot_admin_token)
        if update.alert_smtp_security is not None:
            security = update.alert_smtp_security.strip().lower()
            if security not in ("starttls", "ssl", "none"):
                raise HTTPException(
                    status_code=400,
                    detail="alert_smtp_security muss 'starttls', 'ssl' oder 'none' sein",
                )
            config.alert_smtp_security = security
        if update.alert_mail_cooldown_hours is not None:
            config.alert_mail_cooldown_hours = max(0, int(update.alert_mail_cooldown_hours))

        # ── Fernzugriff ──
        if update.remote_control_enabled is not None:
            config.remote_control_enabled = update.remote_control_enabled
        if update.remote_allow_actions is not None:
            config.remote_allow_actions = update.remote_allow_actions
        if update.remote_poll_seconds is not None:
            # Unter 5 Sekunden ist die Poll-Schleife nur noch Last ohne Nutzen
            config.remote_poll_seconds = max(5, int(update.remote_poll_seconds))

        config.save()
        logger.info("Configuration updated")

        # Ohne das wirkten geänderte Upload-Zeiten erst nach einem Neustart des
        # Dienstes — und bis dahin lief still der alte Zeitplan weiter.
        if update.sync_times is not None:
            try:
                get_scheduler().reload_sync_jobs()
            except Exception as e:
                logger.error(f"Upload-Termine konnten nicht neu gesetzt werden: {e}")

        return {
            "status": "updated",
            "config": {
                "server_name": config.server_name,
                "sync_times": config.sync_times,
                "blacklist": config.blacklist,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Fernzugriff ───────────────────────────────────────────────────────


@app.get("/api/remote/status")
async def get_remote_status():
    """Zustand des Fernzugriffs plus die offenen Aufträge dieses Macs."""
    import asyncio

    try:
        return await asyncio.to_thread(remote_agent.get_agent_state)
    except Exception as e:
        logger.error(f"Fernzugriff-Status nicht abrufbar: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/diag/log-upload")
async def run_log_upload_diagnosis():
    """Analysiert die Log-Upload-Kette. Rein lesend — lädt nichts hoch."""
    import asyncio

    try:
        return await asyncio.to_thread(diagnostics.diagnose_log_upload)
    except Exception as e:
        logger.error(f"Upload-Diagnose fehlgeschlagen: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Statistik-Uploads ─────────────────────────────────────────────────


@app.get("/api/stats/settings")
async def get_stats_settings():
    """Settings and state of both statistics uploads."""
    try:
        config = get_config()
        return {
            "dashboard": {
                "enabled": config.dashboard_stats_enabled,
                "url": config.supabase_url,
                "key_masked": _mask_key(config.supabase_key),
                "tables": {
                    "device": config.dashboard_table_device,
                    "profile": config.dashboard_table_profile,
                    "stats": config.dashboard_table_stats,
                    "bin": config.dashboard_table_bin,
                },
            },
            "customer": {
                "enabled": config.customer_stats_enabled,
                "source": config.customer_stats_source,
                "session_limit": config.customer_stats_session_limit,
                "auto_disable_legacy_upload": config.auto_disable_legacy_upload,
                "stats": {
                    "url": config.customer_stats_url,
                    "key_masked": _mask_key(config.customer_stats_key),
                    "table": config.customer_stats_table,
                    "schema": config.customer_stats_schema,
                },
                "users": {
                    "url": config.customer_users_url,
                    "key_masked": _mask_key(config.customer_users_key),
                    "table": config.customer_users_table,
                    "schema": config.customer_users_schema,
                },
                "unavailable_fields": SUPERDB_UNAVAILABLE_FIELDS,
            },
            "alerts": {
                "integrity_check_enabled": config.db_integrity_check_enabled,
                "mail_to": config.alert_mail_to,
                "mail_from": config.alert_mail_from,
                "smtp_host": config.alert_smtp_host,
                "smtp_port": config.alert_smtp_port,
                "smtp_user": config.alert_smtp_user,
                "password_masked": _mask_key(config.alert_smtp_password),
                "smtp_security": config.alert_smtp_security,
                "cooldown_hours": config.alert_mail_cooldown_hours,
                "db_path": str(Path(config.sqlite_db_path).expanduser()),
            },
            "bot": {"admin_token_masked": _mask_key(config.bot_admin_token)},
            "legacy_upload": legacy_upload.get_status(),
            "last_runs": run_state.get_runs(),
        }
    except Exception as e:
        logger.error(f"Failed to get stats settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/alert/test")
async def send_test_alert():
    """Send a test mail so the SMTP settings can be verified without a defect."""
    import asyncio

    config = get_config()
    result = await asyncio.to_thread(
        mailer.send_alert,
        f"[mactool] {config.server_name}: Testmail",
        f"Testmail vom Mactool auf {config.server_name}.\n\n"
        f"Wenn diese Mail ankommt, erreicht auch die Warnung bei einer beschädigten "
        f"Datenbank ihr Ziel.\n"
        f"Zeitpunkt: {datetime.now():%Y-%m-%d %H:%M:%S}\n",
    )
    if result.get("status") != "sent":
        raise HTTPException(status_code=400, detail=result.get("error", "Versand fehlgeschlagen"))
    return result


@app.post("/api/db/integrity-check")
async def run_integrity_check():
    """Run the integrity check on demand — checks only, never mails, never uploads."""
    import asyncio

    config = get_config()
    db_path = Path(config.sqlite_db_path).expanduser()
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Datenbank nicht gefunden: {db_path}")
    return await asyncio.to_thread(db_integrity.check_database, db_path)


@app.post("/api/stats/dashboard/run")
async def run_dashboard_stats():
    """Run the dashboard sync now (works regardless of the toggle)."""
    import asyncio

    try:
        return await asyncio.to_thread(trigger_sync, True)
    except Exception as e:
        logger.error(f"Dashboard statistics run failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stats/customer/run")
async def run_customer_stats():
    """Run the customer statistics upload now (works regardless of the toggle)."""
    import asyncio

    try:
        return await asyncio.to_thread(run_customer_stats_upload, "manual")
    except Exception as e:
        logger.error(f"Customer statistics run failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stats/customer/preview")
async def preview_customer_stats():
    """Dry run: collect and map the data without uploading anything."""
    import asyncio

    try:
        return await asyncio.to_thread(run_customer_stats_upload, "preview", True)
    except Exception as e:
        logger.error(f"Customer statistics preview failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stats/test")
async def test_stats_target(req: TargetRequest):
    """Check reachability and columns of an upload target."""
    import asyncio

    try:
        if req.target == "dashboard":
            return await asyncio.to_thread(check_dashboard_target)
        if req.target in ("customer_stats", "customer_users"):
            return await asyncio.to_thread(check_customer_target, req.target)
        raise HTTPException(status_code=400, detail=f"Unbekanntes Ziel: {req.target}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Target test failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/legacy-upload")
async def get_legacy_upload():
    """List LaunchAgents that start a legacy upload script."""
    return legacy_upload.get_status()


@app.post("/api/legacy-upload/disable")
async def disable_legacy_upload(req: AgentRequest):
    """Disable a legacy upload LaunchAgent (reversible)."""
    import asyncio

    result = await asyncio.to_thread(legacy_upload.disable_agent, req.path)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/legacy-upload/enable")
async def enable_legacy_upload(req: AgentRequest):
    """Re-enable a previously disabled legacy upload LaunchAgent."""
    import asyncio

    result = await asyncio.to_thread(legacy_upload.enable_agent, req.path)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/bot/local-test")
async def bot_local_test():
    """Lesend prüfen, ob die lokale Bot-App-Schnittstelle das Admin-Token annimmt."""
    import bot_local

    return bot_local.test_connection()


@app.post("/api/bot/restart")
async def restart_bot_endpoint():
    """Restart Bot.app."""
    import asyncio
    try:
        success = await asyncio.to_thread(restart_bot)
        return {
            "status": "success" if success else "failed",
            "running": is_bot_running(),
        }
    except Exception as e:
        logger.error(f"Bot restart failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bot/start")
async def start_bot_endpoint():
    """Start Bot.app."""
    import asyncio
    try:
        success = await asyncio.to_thread(start_bot)
        return {
            "status": "success" if success else "failed",
            "running": is_bot_running(),
        }
    except Exception as e:
        logger.error(f"Bot start failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bot/stop")
async def stop_bot_endpoint():
    """Stop Bot.app."""
    import asyncio
    try:
        success = await asyncio.to_thread(stop_bot)
        return {
            "status": "success" if success else "failed",
            "running": is_bot_running(),
        }
    except Exception as e:
        logger.error(f"Bot stop failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rustdesk/start")
async def start_rustdesk_endpoint():
    """Enable the RustDesk watchdog and launch RustDesk now."""
    import asyncio
    try:
        set_rustdesk_watch_enabled(True)
        success = await asyncio.to_thread(start_rustdesk)
        return {
            "status": "success" if success else "failed",
            "running": is_rustdesk_running(),
            "watch": is_rustdesk_watch_enabled(),
        }
    except Exception as e:
        logger.error(f"RustDesk start failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rustdesk/stop")
async def stop_rustdesk_endpoint():
    """Disable the RustDesk watchdog (does NOT kill a running RustDesk)."""
    try:
        set_rustdesk_watch_enabled(False)
        return {
            "status": "success",
            "running": is_rustdesk_running(),
            "watch": is_rustdesk_watch_enabled(),
        }
    except Exception as e:
        logger.error(f"RustDesk watchdog disable failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/update/check")
async def check_update():
    """Check if an update is available."""
    return check_for_updates()


@app.get("/api/versions")
async def list_versions():
    """List all available version tags."""
    versions = get_available_versions()
    current = get_current_version()
    return {"current": current, "versions": versions}


class UpdateRequest(BaseModel):
    version: str | None = None


@app.post("/api/update")
async def do_update(req: UpdateRequest = UpdateRequest()):
    """Update to a specific version or latest."""
    try:
        result = perform_update(version=req.version)
        return result
    except Exception as e:
        logger.error(f"Update failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
