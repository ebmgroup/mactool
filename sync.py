"""SQLite to Supabase synchronization with schema discovery and column mapping."""

import sqlite3
import shutil
import logging
import re
import requests
from pathlib import Path
from datetime import datetime
import time

from supabase import create_client, Client
from config import get_config
from log_uploader import upload_bot_logs
from supabase_rest import describe_target
import db_integrity
import run_state

logger = logging.getLogger(__name__)

# PostgreSQL maximum identifier length
PG_MAX_IDENTIFIER = 63


def get_table_mappings() -> dict[str, str]:
    """SQLite table → Supabase table, taken from the configured names."""
    config = get_config()
    return {
        "device": config.dashboard_table_device,
        "profile": config.dashboard_table_profile,
        "stats": config.dashboard_table_stats,
    }


class SyncError(Exception):
    """Sync operation error."""

    pass


class SyncManager:
    """Manages SQLite to Supabase synchronization."""

    def __init__(self):
        config = get_config()
        self.server_prefix = config.server_name
        self.db_path = Path(config.sqlite_db_path).expanduser()
        self.supabase_url = config.supabase_url
        self.supabase_key = config.supabase_key
        self.sb_client: Client | None = None
        # Per-sync caches (reset at start of each sync)
        self._supabase_columns: dict[str, set[str]] = {}
        self._skipped_columns: dict[str, list[str]] = {}
        self._column_maps: dict[str, dict[str, str]] = {}
        self._init_supabase_client()

    def _init_supabase_client(self):
        """Initialize Supabase client."""
        if not self.supabase_key:
            raise SyncError(
                "SUPABASE_KEY not configured. Set it in config.json or SUPABASE_KEY env var."
            )
        try:
            self.sb_client = create_client(self.supabase_url, self.supabase_key)
            logger.info(f"Supabase client initialized: {self.supabase_url}")
        except Exception as e:
            logger.error(f"Failed to initialize Supabase client: {e}")
            raise SyncError(f"Supabase init failed: {e}")

    def _integrity_gate(self, upload_all_logs: bool) -> dict | None:
        """Run the integrity check. Returns a sync result when the sync must stop."""
        config = get_config()
        if not config.db_integrity_check_enabled:
            return None
        if not self.db_path.exists():
            # Kein Defekt, sondern schlicht nichts da — _create_temp_db meldet "no_db"
            return None

        check = db_integrity.verify_before_upload(self.db_path, self.server_prefix)
        if check["ok"]:
            return None

        error = f"Integrity Check der Datenbank fehlgeschlagen: {check['detail']}"
        logger.error(f"{error} — kein Upload")
        run_state.record_run(
            "dashboard_stats",
            "integrity_failed",
            error,
            "manual" if upload_all_logs else "auto",
            {"integrity": check},
        )
        return {
            "status": "integrity_failed",
            "tables": {},
            "integrity": check,
            "error": error,
        }

    def _create_temp_db(self) -> Path:
        """Create temporary copy of SQLite database."""
        try:
            if not self.db_path.exists():
                logger.warning(f"Database {self.db_path} not found.")
                return None

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            temp_path = Path(f"/tmp/super_{ts}.db")
            shutil.copy2(self.db_path, temp_path)
            logger.info(f"Created temp DB copy: {temp_path}")
            return temp_path
        except Exception as e:
            logger.error(f"Failed to create temp DB: {e}")
            raise SyncError(f"Temp DB creation failed: {e}")

    # ── Schema Discovery ──────────────────────────────────────────────

    def _discover_supabase_columns(self, table_name: str) -> set[str]:
        """Discover columns in a Supabase table.

        Uses two methods:
        1. OpenAPI spec (works even for empty tables)
        2. Fallback: SELECT one row to read column names
        """
        headers = {
            "apikey": self.supabase_key,
            "Authorization": f"Bearer {self.supabase_key}",
        }

        # Method 1: OpenAPI spec at /rest/v1/
        try:
            resp = requests.get(
                f"{self.supabase_url}/rest/v1/",
                headers=headers,
                timeout=15,
            )
            if resp.status_code == 200:
                schema = resp.json()
                definitions = schema.get("definitions", {})
                if table_name in definitions:
                    props = definitions[table_name].get("properties", {})
                    cols = set(props.keys())
                    if cols:
                        logger.info(
                            f"Discovered {len(cols)} columns in Supabase '{table_name}' via OpenAPI"
                        )
                        return cols
        except Exception as e:
            logger.debug(f"OpenAPI discovery failed for '{table_name}': {e}")

        # Method 2: Query one row
        try:
            response = (
                self.sb_client.table(table_name).select("*").limit(1).execute()
            )
            if response.data:
                cols = set(response.data[0].keys())
                logger.info(
                    f"Discovered {len(cols)} columns in Supabase '{table_name}' via query"
                )
                return cols
        except Exception as e:
            logger.debug(f"Query discovery failed for '{table_name}': {e}")

        logger.warning(f"Could not discover columns for Supabase '{table_name}'")
        return set()

    def _build_column_map(
        self, table_name: str, sqlite_cols: list[str], supabase_cols: set[str]
    ) -> dict[str, str]:
        """Build mapping from SQLite column names to Supabase column names.

        Handles:
        - Direct matches (same name)
        - Truncated matches (SQLite name >63 chars, Supabase has truncated version)
        - Tracks skipped columns (not found in Supabase)
        """
        col_map: dict[str, str] = {}  # sqlite_name → supabase_name
        skipped: list[str] = []

        if not supabase_cols:
            # No schema info available — send everything with truncated names
            for col in sqlite_cols:
                col_map[col] = col[:PG_MAX_IDENTIFIER]
            logger.warning(
                f"No Supabase schema for '{table_name}', sending all {len(col_map)} columns "
                f"(names truncated to {PG_MAX_IDENTIFIER} chars)"
            )
            return col_map

        for col in sqlite_cols:
            # 1. Direct match
            if col in supabase_cols:
                col_map[col] = col
            # 2. Truncated match (for columns > 63 chars)
            elif len(col) > PG_MAX_IDENTIFIER:
                truncated = col[:PG_MAX_IDENTIFIER]
                if truncated in supabase_cols:
                    col_map[col] = truncated
                    logger.info(f"Column mapped: '{col}' → '{truncated}'")
                else:
                    skipped.append(col)
            else:
                skipped.append(col)

        self._skipped_columns[table_name] = skipped

        if skipped:
            logger.info(
                f"Column mapping for '{table_name}': "
                f"{len(col_map)} mapped, {len(skipped)} skipped "
                f"(not in Supabase)"
            )
            if len(skipped) <= 10:
                for s in skipped:
                    logger.debug(f"  Skipped: {s}")

        return col_map

    # ── Record Preparation ────────────────────────────────────────────

    def _get_table_columns(self, db_conn: sqlite3.Connection, table: str) -> list[str]:
        """Get all column names from a SQLite table."""
        cursor = db_conn.cursor()
        cursor.execute(f"PRAGMA table_info({table});")
        cols = cursor.fetchall()
        return [col[1] for col in cols]

    def _query_table(self, db_conn: sqlite3.Connection, table: str) -> list[dict]:
        """Query all records from a SQLite table."""
        cursor = db_conn.cursor()
        cols = self._get_table_columns(db_conn, table)
        cols_str = ", ".join(f'"{col}"' for col in cols)

        if table == "stats":
            query = f"""
                SELECT {cols_str} FROM {table}
                WHERE date >= date('now', '-90 days')
                ORDER BY date DESC;
            """
        else:
            query = f"SELECT {cols_str} FROM {table};"

        cursor.execute(query)
        rows = cursor.fetchall()

        result = [dict(zip(cols, row)) for row in rows]
        logger.info(f"Queried '{table}': {len(result)} rows, {len(cols)} columns")
        return result

    @staticmethod
    def _sanitize_value(value):
        """Sanitize a value for Supabase/PostgreSQL compatibility.

        - Strip \\x00 NULL bytes from strings (PostgreSQL rejects them: error 22P05)
        - Convert bytes to string
        - Leave None, int, float as-is
        """
        if isinstance(value, str):
            if "\x00" in value:
                return value.replace("\x00", "")
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace").replace("\x00", "")
        return value

    def _enrich_record(self, record: dict) -> dict:
        """Add metadata fields, prefix IDs, and sanitize values."""
        result = {}
        for k, v in record.items():
            result[k] = self._sanitize_value(v)

        result["mac_id"] = self.server_prefix
        result["ig_server"] = self.server_prefix
        result["imported_at"] = datetime.now().isoformat()
        result["change_at"] = None

        if "id" in result and result["id"]:
            result["id"] = f"{self.server_prefix}_{result['id']}"

        if "profileID" in result and result["profileID"]:
            result["profileID"] = f"{self.server_prefix}_{result['profileID']}"

        return result

    def _prepare_records(
        self, table_name: str, records: list[dict]
    ) -> list[dict]:
        """Map SQLite records to Supabase-compatible format.

        - Discovers Supabase columns (cached per sync)
        - Maps column names (truncation for >63 chars)
        - Filters to only columns that exist in Supabase
        """
        if not records:
            return records

        # Discover Supabase columns (once per table per sync)
        if table_name not in self._supabase_columns:
            self._supabase_columns[table_name] = self._discover_supabase_columns(
                table_name
            )

        supabase_cols = self._supabase_columns[table_name]
        sqlite_cols = list(records[0].keys())

        # Build column mapping (once per table per sync)
        if table_name not in self._column_maps:
            self._column_maps[table_name] = self._build_column_map(
                table_name, sqlite_cols, supabase_cols
            )

        col_map = self._column_maps[table_name]

        # Apply mapping to all records
        prepared = []
        for record in records:
            new_record = {}
            for sqlite_col, sb_col in col_map.items():
                if sqlite_col in record:
                    new_record[sb_col] = record[sqlite_col]
            prepared.append(new_record)

        if prepared:
            logger.info(
                f"Prepared '{table_name}': {len(sqlite_cols)} → {len(prepared[0])} columns"
            )

        return prepared

    # ── Batch Upsert ──────────────────────────────────────────────────

    def _extract_bad_column(
        self, error: Exception, batch_keys: set[str], table_name: str
    ) -> str | None:
        """Extract the bad column name from a Supabase error message.

        Strategy:
        1. Find all single-quoted strings in the error
        2. Skip any that match the table name (avoid confusing table with column)
        3. Prefer candidates that exist as keys in the current batch
        4. Fallback to first non-table candidate
        """
        error_str = str(error)
        candidates = re.findall(r"'([^']+)'", error_str)

        # Filter out the table name
        non_table = [c for c in candidates if c != table_name]

        # Prefer candidates that are actual batch keys
        for candidate in non_table:
            if candidate in batch_keys:
                return candidate

        # Fallback: first non-table candidate
        if non_table:
            return non_table[0]

        # Last resort: first candidate at all
        if candidates:
            return candidates[0]

        return None

    def _batch_upsert(
        self, table_name: str, records: list[dict], batch_size: int = 500
    ):
        """Batch UPSERT records to Supabase with error recovery.

        When a column-not-found error occurs, the bad column is removed
        from the current batch AND all subsequent batches (via
        `_removed_columns` tracking).
        """
        if not records:
            logger.info(f"No records to sync for '{table_name}'")
            return

        total_batches = (len(records) + batch_size - 1) // batch_size
        removed_columns: set[str] = set()  # Tracks columns removed across all batches

        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            batch_num = i // batch_size + 1
            retry_count = 0
            max_retries = 3

            # Apply previously-removed columns to this batch
            if removed_columns:
                current_batch = [
                    {k: v for k, v in rec.items() if k not in removed_columns}
                    for rec in batch
                ]
            else:
                current_batch = batch

            while retry_count < max_retries:
                try:
                    self.sb_client.table(table_name).upsert(
                        current_batch, on_conflict="id"
                    ).execute()
                    logger.info(
                        f"Upserted {len(current_batch)} records to '{table_name}' "
                        f"(batch {batch_num}/{total_batches})"
                    )

                    # After successful upsert with removals, update schema cache
                    if current_batch and removed_columns:
                        self._supabase_columns[table_name] = set(
                            current_batch[0].keys()
                        )
                        if table_name in self._column_maps:
                            del self._column_maps[table_name]

                    break

                except Exception as e:
                    error_msg = str(e).lower()

                    # Handle unique constraint violations (e.g. mac_id has UNIQUE)
                    if "23505" in error_msg or "unique constraint" in error_msg:
                        # Extract the constraint name to give a clear fix instruction
                        constraint_match = re.search(
                            r'"([^"]+)"', str(e)
                        )
                        constraint = constraint_match.group(1) if constraint_match else "unknown"
                        col_match = re.search(r"Key \(([^)]+)\)", str(e))
                        col = col_match.group(1) if col_match else "unknown"

                        fix_sql = (
                            f'ALTER TABLE "{table_name}" DROP CONSTRAINT "{constraint}";'
                            if constraint != "unknown"
                            else f"-- Check unique constraints on '{table_name}'"
                        )
                        raise SyncError(
                            f"Unique constraint '{constraint}' on column '{col}' "
                            f"blocks multi-row insert. "
                            f"Fix in Supabase SQL Editor: {fix_sql}"
                        )

                    # Handle column-not-found errors (fallback for undiscovered schema)
                    if "column" in error_msg and (
                        "not found" in error_msg
                        or "does not exist" in error_msg
                        or "undefined" in error_msg
                        or "schema cache" in error_msg
                    ):
                        batch_keys = set(current_batch[0].keys()) if current_batch else set()
                        bad_col = self._extract_bad_column(e, batch_keys, table_name)
                        if bad_col:
                            logger.warning(
                                f"Column '{bad_col}' not in Supabase '{table_name}', removing"
                            )
                            removed_columns.add(bad_col)
                            current_batch = [
                                {k: v for k, v in rec.items() if k != bad_col}
                                for rec in current_batch
                            ]

                            # Safety: don't loop forever removing columns
                            if len(removed_columns) > 50:
                                raise SyncError(
                                    f"Too many missing columns in '{table_name}' "
                                    f"({len(removed_columns)} removed). Check Supabase schema."
                                )
                            continue

                    # Other errors: retry with exponential backoff
                    retry_count += 1
                    wait_time = 2**retry_count
                    logger.warning(
                        f"Upsert error on '{table_name}' "
                        f"(attempt {retry_count}/{max_retries}): {e}"
                    )
                    if retry_count >= max_retries:
                        raise SyncError(
                            f"Upsert failed for '{table_name}' after {max_retries} retries: {e}"
                        )
                    time.sleep(wait_time)

    # ── Bin Table Sync ───────────────────────────────────────────────

    def _sync_bin_table(self, db_conn: sqlite3.Connection) -> dict:
        """Sync the 'bin' table with only id, config__username, noneoption__email.

        After upserting current records, removes stale rows from Supabase
        that no longer exist in the local bin table for this mac prefix.
        """
        sb_table = get_config().dashboard_table_bin
        result = {"status": "success", "count": 0, "deleted_stale": 0}

        # Query only the 3 columns from SQLite
        try:
            cursor = db_conn.cursor()
            cursor.execute(
                'SELECT "id", "config__username", "noneoption__email" FROM bin;'
            )
            rows = cursor.fetchall()
        except Exception as e:
            logger.warning(f"Could not query 'bin' table: {e}")
            result["status"] = "no_table"
            return result

        if not rows:
            logger.info("bin: No records to sync")
            result["status"] = "no_data"
            return result

        # Build records with prefixed IDs
        local_ids = set()
        records = []
        for row in rows:
            raw_id, username, email = row
            prefixed_id = f"{self.server_prefix}_{raw_id}"
            local_ids.add(prefixed_id)
            records.append({
                "id": prefixed_id,
                "config__username": self._sanitize_value(username),
                "noneoption__email": self._sanitize_value(email),
            })

        logger.info(f"bin: {len(records)} records to sync")

        # Upsert current records
        self._batch_upsert(sb_table, records)
        result["count"] = len(records)

        # Cleanup: delete stale rows for this mac prefix
        try:
            prefix_pattern = f"{self.server_prefix}_%"
            response = (
                self.sb_client.table(sb_table)
                .select("id")
                .like("id", prefix_pattern)
                .execute()
            )
            remote_ids = {r["id"] for r in response.data} if response.data else set()
            stale_ids = remote_ids - local_ids

            if stale_ids:
                # Batch delete in chunks to avoid one-request-per-id and huge URLs
                stale_list = list(stale_ids)
                chunk_size = 200
                for i in range(0, len(stale_list), chunk_size):
                    chunk = stale_list[i : i + chunk_size]
                    self.sb_client.table(sb_table).delete().in_("id", chunk).execute()
                result["deleted_stale"] = len(stale_ids)
                logger.info(f"bin: Deleted {len(stale_ids)} stale records for {self.server_prefix}")
            else:
                logger.info(f"bin: No stale records to clean up")

        except Exception as e:
            logger.warning(f"bin: Stale record cleanup failed (non-fatal): {e}")

        return result

    # ── Main Sync ─────────────────────────────────────────────────────

    def sync(self, upload_all_logs: bool = False) -> dict:
        """Execute full sync: SQLite → Supabase.

        upload_all_logs: if True (manual "Sync Now"), upload every Phone-device
        log file, not just the previous 2h timeslot. Scheduled/auto syncs pass
        False to keep burst size small.
        """
        logger.info("=" * 60)
        logger.info(
            f"Starting SQLite → Supabase sync... (upload_all_logs={upload_all_logs})"
        )

        # Eine beschädigte super.db wird nicht hochgeladen. Die Prüfung läuft auf
        # dem Original, bevor die Arbeitskopie gezogen wird — eine kaputte Datei
        # zu kopieren hilft niemandem. Eine fehlende DB ist kein Defekt und wird
        # weiter unten als "no_db" behandelt, ohne jemanden per Mail zu wecken.
        integrity_abort = self._integrity_gate(upload_all_logs)
        if integrity_abort:
            return integrity_abort

        # Reset per-sync caches
        self._supabase_columns = {}
        self._skipped_columns = {}
        self._column_maps = {}

        sync_result = {"status": "success", "tables": {}}
        temp_db_path = None

        try:
            temp_db_path = self._create_temp_db()
            if not temp_db_path:
                logger.warning("No database found. Skipping sync.")
                sync_result["status"] = "no_db"
                return sync_result

            db_conn = sqlite3.connect(temp_db_path)

            config = get_config()
            stats_enabled = config.dashboard_stats_enabled

            for table_key, sb_table in get_table_mappings().items():
                try:
                    # "Statistik (Dashboard)" only gates the stats table — device,
                    # profile, bin and the log upload keep running either way.
                    if table_key == "stats" and not stats_enabled:
                        sync_result["tables"][table_key] = {
                            "status": "disabled",
                            "count": 0,
                        }
                        logger.info("  stats: Statistik (Dashboard) ist aus — übersprungen")
                        continue

                    logger.info(f"--- Syncing table: {table_key} → {sb_table} ---")

                    records = self._query_table(db_conn, table_key)
                    if not records:
                        sync_result["tables"][table_key] = {
                            "status": "no_data",
                            "count": 0,
                        }
                        logger.info(f"  {table_key}: No records to sync")
                        continue

                    # Enrich with metadata
                    enriched = [self._enrich_record(rec) for rec in records]

                    # Map columns to Supabase schema
                    prepared = self._prepare_records(sb_table, enriched)

                    # Upsert to Supabase
                    self._batch_upsert(sb_table, prepared)

                    table_result = {
                        "status": "success",
                        "count": len(records),
                    }
                    if sb_table in self._skipped_columns:
                        table_result["skipped_columns"] = len(
                            self._skipped_columns[sb_table]
                        )
                    if sb_table in self._column_maps:
                        table_result["columns_synced"] = len(
                            self._column_maps[sb_table]
                        )

                    sync_result["tables"][table_key] = table_result
                    logger.info(
                        f"  {table_key}: {len(records)} records synced successfully"
                    )

                except SyncError as e:
                    logger.error(f"Sync failed for {table_key}: {e}")
                    sync_result["tables"][table_key] = {
                        "status": "error",
                        "error": str(e)[:500],
                    }
                    sync_result["status"] = "partial_error"
                except Exception as e:
                    logger.error(f"Unexpected error for {table_key}: {e}", exc_info=True)
                    sync_result["tables"][table_key] = {
                        "status": "error",
                        "error": f"{type(e).__name__}: {str(e)[:500]}",
                    }
                    sync_result["status"] = "partial_error"

            # Sync bin table (separate: only 3 columns + stale cleanup)
            try:
                logger.info("--- Syncing table: bin → bin ---")
                bin_result = self._sync_bin_table(db_conn)
                sync_result["tables"]["bin"] = bin_result
                logger.info(
                    f"  bin: {bin_result.get('count', 0)} records synced, "
                    f"{bin_result.get('deleted_stale', 0)} stale deleted"
                )
            except Exception as e:
                logger.error(f"bin sync failed (non-fatal): {e}")
                sync_result["tables"]["bin"] = {
                    "status": "error",
                    "error": str(e)[:500],
                }

            db_conn.close()

            # Upload bot logs to Supabase Storage
            try:
                log_result = upload_bot_logs(
                    self.sb_client,
                    self.server_prefix,
                    self.db_path.parent,
                    temp_db_path,
                    upload_all=upload_all_logs,
                )
                sync_result["log_upload"] = log_result
                if log_result.get("status") == "error":
                    logger.warning(f"Log upload had errors: {log_result}")
                else:
                    logger.info(
                        f"Log upload: {log_result.get('uploaded', 0)} uploaded, "
                        f"{log_result.get('cleaned', 0)} cleaned"
                    )
            except Exception as e:
                logger.error(f"Log upload failed (non-fatal): {e}")
                sync_result["log_upload"] = {
                    "status": "error",
                    "error": str(e)[:500],
                }

            # Warn-/Crash-Archive des Bots (Screenshots fuer SK TEST 3, 7 Tage)
            # und lokales Aufraeumen nach 60 Tagen — siehe archive_uploader.py.
            try:
                from archive_uploader import run_archive

                sync_result["archive"] = run_archive(
                    self.sb_client,
                    self.server_prefix,
                    self.db_path.parent,
                    self.supabase_url,
                    self.supabase_key,
                )
                logger.info(f"Archiv: {sync_result['archive']}")
            except Exception as e:
                logger.error(f"Archiv fehlgeschlagen (nicht schlimm): {e}")
                sync_result["archive"] = {"status": "error", "error": str(e)[:500]}

            # Summary
            if self._skipped_columns:
                sync_result["skipped_columns"] = {
                    table: cols for table, cols in self._skipped_columns.items()
                }
            if self._column_maps:
                sync_result["column_mapping"] = {
                    table: len(m) for table, m in self._column_maps.items()
                }

            logger.info("Sync completed successfully")
            logger.info("=" * 60)

        except SyncError as e:
            logger.error(f"Sync aborted: {e}")
            sync_result["status"] = "error"
            sync_result["error"] = str(e)

        except Exception as e:
            logger.error(f"Unexpected sync error: {e}", exc_info=True)
            sync_result["status"] = "error"
            sync_result["error"] = str(e)

        finally:
            if temp_db_path and temp_db_path.exists():
                try:
                    temp_db_path.unlink()
                    logger.debug(f"Deleted temp DB: {temp_db_path}")
                except Exception as e:
                    logger.warning(f"Failed to delete temp DB: {e}")

        summary = ", ".join(
            f"{table}={info.get('count', info.get('status'))}"
            for table, info in sync_result.get("tables", {}).items()
        )
        run_state.record_run(
            "dashboard_stats",
            sync_result.get("status", "unknown"),
            summary or sync_result.get("error", ""),
            "manual" if upload_all_logs else "auto",
        )

        return sync_result


# Singleton instance
_sync_manager: SyncManager | None = None


def get_sync_manager() -> SyncManager:
    """Get singleton SyncManager instance.

    Rebuilt when the configured Supabase target, server name or database path
    changed, so edits in the dashboard settings take effect without a restart.
    """
    global _sync_manager
    config = get_config()
    if (
        _sync_manager is None
        or _sync_manager.supabase_url != config.supabase_url
        or _sync_manager.supabase_key != config.supabase_key
        or _sync_manager.server_prefix != config.server_name
        or _sync_manager.db_path != Path(config.sqlite_db_path).expanduser()
    ):
        _sync_manager = SyncManager()
    return _sync_manager


# Metadata columns _enrich_record adds to every record
DASHBOARD_META_COLUMNS = {
    "mac_id": "text",
    "ig_server": "text",
    "imported_at": "timestamp",
    "change_at": "timestamp",
}

BIN_COLUMNS = {
    "id": "text",
    "config__username": "text",
    "noneoption__email": "text",
}


def _local_table_columns() -> dict[str, list[str]]:
    """Read the column names of the local SQLite tables."""
    config = get_config()
    db_path = Path(config.sqlite_db_path).expanduser()
    if not db_path.exists():
        return {}

    temp_path = Path(f"/tmp/schema_check_{datetime.now():%Y%m%d_%H%M%S}.db")
    columns: dict[str, list[str]] = {}
    try:
        shutil.copy2(db_path, temp_path)
        conn = sqlite3.connect(temp_path)
        try:
            for table in ("device", "profile", "stats"):
                try:
                    columns[table] = [
                        row[1] for row in conn.execute(f"PRAGMA table_info({table});")
                    ]
                except Exception as e:
                    logger.debug(f"PRAGMA failed for '{table}': {e}")
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Could not inspect local schema: {e}")
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass

    return columns


def check_dashboard_target() -> dict:
    """Connection test for the dashboard Supabase (per table)."""
    config = get_config()

    if not config.supabase_url or not config.supabase_key:
        return {
            "status": "not_configured",
            "error": "Supabase URL oder Key fehlt",
            "targets": [],
        }

    local_columns = _local_table_columns()
    tables = get_table_mappings()
    tables["bin"] = config.dashboard_table_bin

    targets = []
    for source_table, sb_table in tables.items():
        if source_table == "bin":
            expected = dict(BIN_COLUMNS)
        else:
            expected = {
                col[:PG_MAX_IDENTIFIER]: "text"
                for col in local_columns.get(source_table, [])
            }
            expected.update(DASHBOARD_META_COLUMNS)

        info = describe_target(
            config.supabase_url, config.supabase_key, sb_table, expected or None
        )
        info["source_table"] = source_table
        if source_table == "stats" and not config.dashboard_stats_enabled:
            info["note"] = "Statistik (Dashboard) ist aktuell ausgeschaltet"
        if not local_columns.get(source_table) and source_table != "bin":
            info["note"] = "Lokale super.db nicht lesbar — Spaltenabgleich übersprungen"
        targets.append(info)

    statuses = [t["status"] for t in targets]
    if "error" in statuses:
        status = "error"
    elif "incomplete" in statuses:
        status = "incomplete"
    else:
        status = "ok"

    return {"status": status, "targets": targets}


def trigger_sync(upload_all_logs: bool = False) -> dict:
    """Trigger a sync operation.

    upload_all_logs: pass True for manual triggers (button / CLI) so that every
    Phone log file is uploaded, not just the previous 2h timeslot.
    """
    try:
        manager = get_sync_manager()
        return manager.sync(upload_all_logs=upload_all_logs)
    except Exception as e:
        logger.error(f"Sync trigger failed: {e}")
        return {"status": "error", "error": str(e)}
