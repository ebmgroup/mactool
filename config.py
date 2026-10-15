"""Configuration management for mactool."""

import json
import os
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG_FILE = Path(__file__).parent / "config.json.example"

MASK_CHAR = "•"

# Alles, was nie im Klartext das Gerät verlassen darf — weder über das Dashboard
# noch über den Fernzugriff. Die webhook_url gehört dazu: ihre ID *ist* das Geheimnis.
SECRET_FIELDS = frozenset(
    {
        "supabase_key",
        "customer_stats_key",
        "customer_users_key",
        "alert_smtp_password",
        "webhook_url",
        "bot_admin_token",
    }
)


def mask_key(value: str) -> str:
    """Show only the last 4 characters of a key."""
    if not value:
        return ""
    if len(value) <= 4:
        return MASK_CHAR * 8
    return MASK_CHAR * 8 + value[-4:]


@dataclass
class AppConfig:
    """Application configuration."""

    server_name: str
    sync_times: list[str] = field(default_factory=lambda: ["09:00", "14:30"])
    blacklist: list[str] = field(default_factory=list)
    supabase_url: str = "https://fxreaveeihaawkusmybi.supabase.co"
    supabase_key: str = ""
    bot_app_path: str = "/Applications/botapp.app/Contents/MacOS/BotApp"
    rustdesk_app_path: str = "/Applications/RustDesk.app"
    adb_path: str = "adb"
    sqlite_db_path: str = "~/Desktop/GramBotStorage/super.db"
    webhook_url: str = ""
    device_check_interval_hours: int = 1
    bot_check_interval_minutes: int = 5
    rustdesk_check_interval_minutes: int = 5
    log_level: str = "INFO"
    github_repo: str = ""  # e.g. "username/mactool" — for self-update

    # ── Statistik (Dashboard) ────────────────────────────────────────
    # Target is supabase_url / supabase_key above. The toggle only gates the
    # `stats` table — device, profile, bin and the bot log upload keep running.
    dashboard_stats_enabled: bool = True
    dashboard_table_device: str = "device"
    dashboard_table_profile: str = "profile"
    dashboard_table_stats: str = "stats"
    dashboard_table_bin: str = "bin"

    # ── Statistik (Kunde) ────────────────────────────────────────────
    # Replaces the per-mac `upload-macXX.py` LaunchAgent scripts. Two targets:
    # raw session data (upsert) and the "beautified" users table (insert).
    customer_stats_enabled: bool = False
    customer_stats_source: str = "sessions"  # "sessions" | "superdb"
    customer_stats_session_limit: int = 90
    customer_stats_url: str = "https://eeshaewrbrqipsvtlyng.supabase.co"
    customer_stats_key: str = ""
    customer_stats_table: str = "statistik"
    customer_stats_schema: str = "public"
    customer_users_url: str = "https://apltfvenhqwnidmuptdp.supabase.co"
    customer_users_key: str = ""
    customer_users_table: str = "users"
    # PostgREST picks its first exposed schema when none is given — which may be
    # a narrow read-only one. Always name the schema the data really lives in.
    customer_users_schema: str = "public"
    # Disable the legacy upload LaunchAgent once our own upload succeeded once
    auto_disable_legacy_upload: bool = True

    # ── DB-Integritätsprüfung + E-Mail-Alarm ─────────────────────────
    # Vor dem Supabase-Sync läuft `PRAGMA integrity_check` auf der super.db.
    # Ist sie beschädigt, wird nichts hochgeladen und eine Mail verschickt —
    # sonst merkt es niemand, die Macs laufen unbeaufsichtigt.
    db_integrity_check_enabled: bool = True
    alert_mail_to: str = "development@ebm-group.de"
    alert_mail_from: str = ""  # leer = alert_smtp_user
    alert_smtp_host: str = ""
    alert_smtp_port: int = 587
    alert_smtp_user: str = ""
    alert_smtp_password: str = ""
    alert_smtp_security: str = "starttls"  # "starttls" | "ssl" | "none"
    # Bleibt die DB kaputt, nicht bei jedem Sync erneut mailen
    alert_mail_cooldown_hours: int = 6

    # ── Fernzugriff ──────────────────────────────────────────────────
    # Die Macs stehen hinter NAT und sind nur per RustDesk erreichbar — für eine
    # Analyse aus der Ferne zu wenig. Statt einen Port zu öffnen holt sich der Mac
    # seine Aufträge selbst aus einer Tabelle der Dashboard-Supabase (`supabase_url`
    # / `supabase_key` oben) und legt die Antwort dort wieder ab.
    #
    # Ab Werk an: sonst müsste man jeden Mac per RustDesk anfassen, um genau das
    # Werkzeug scharfzuschalten, das RustDesk ersparen soll.
    remote_control_enabled: bool = True
    # Aus = nur lesende Befehle. Sync, Bot-/RustDesk-Steuerung und Update werden
    # dann abgelehnt, Status und Diagnose laufen weiter.
    remote_allow_actions: bool = True
    remote_poll_seconds: int = 15

    # IG Versionen: Bestand der Handys melden + Update-Aufträge aus dem Dashboard
    # abarbeiten (ig_worker.py). Ab Werk an — sonst müsste jeder Mac angefasst werden.
    ig_worker_enabled: bool = True
    # Wie viele Phones ein Mac gleichzeitig aktualisiert (alle IG-Aufträge zusammen).
    # Aus der Ferne: macctl.py mac04 set ig_parallel_phones=3
    ig_parallel_phones: int = 5
    # Admin-Token der Bot-App (lokale Schnittstelle, X-Admin-Key). Geheim: nur per Dashboard
    # setzen, nie über den Fernzugriff (SECRET_FIELDS).
    bot_admin_token: str = ""
    remote_commands_table: str = "mac_commands"
    remote_agents_table: str = "mac_agents"

    def save(self):
        """Save configuration to config.json."""
        try:
            with open(CONFIG_FILE, "w") as f:
                json.dump(asdict(self), f, indent=2)
            logger.info(f"Configuration saved to {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            raise

    @staticmethod
    def load() -> "AppConfig":
        """Load configuration from config.json or environment."""

        # Try to load from config.json first
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r") as f:
                    data = json.load(f)

                # Ignore keys this version doesn't know. Without this, running an
                # older version (via the dashboard's version selector) against a
                # config.json written by a newer one raises TypeError and the
                # service falls back to an empty default config.
                known = {f.name for f in fields(AppConfig)}
                unknown = sorted(set(data) - known)
                if unknown:
                    logger.warning(
                        f"Ignoring unknown config keys (newer version?): {', '.join(unknown)}"
                    )

                logger.info(f"Configuration loaded from {CONFIG_FILE}")
                return AppConfig(**{k: v for k, v in data.items() if k in known})
            except Exception as e:
                logger.error(f"Failed to load config from file: {e}")

        # Fallback: try environment variable for supabase_key
        supabase_key = os.getenv("SUPABASE_KEY", "")
        server_name = os.getenv("SERVER_NAME", "default")

        if not supabase_key:
            logger.warning("SUPABASE_KEY not set in environment or config.json")

        config = AppConfig(server_name=server_name, supabase_key=supabase_key)
        logger.info(f"Using configuration with server_name={server_name}")
        return config


_config_instance: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """Get singleton configuration instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = AppConfig.load()
    return _config_instance


def reload_config() -> AppConfig:
    """Reload configuration from file."""
    global _config_instance
    _config_instance = AppConfig.load()
    return _config_instance


def masked_config_dict(config: Optional[AppConfig] = None) -> dict:
    """The whole configuration with every secret replaced by its masked form.

    Whitelist by exclusion on purpose: a field added later is exposed unless it is
    listed in SECRET_FIELDS, so new *secrets* must be registered there. Keep this
    the only way config leaves the machine — two maskings drift apart and one of
    them eventually leaks a key.
    """
    config = config or get_config()
    return {
        name: mask_key(value) if name in SECRET_FIELDS else value
        for name, value in asdict(config).items()
    }
