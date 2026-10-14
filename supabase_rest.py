"""Minimal PostgREST client for the additional Supabase projects.

The customer statistics upload talks to two Supabase projects that are separate
from the dashboard one. Creating a second/third `supabase.create_client()` in the
same process caused problems in the legacy upload script, and we only need a
handful of calls — so this module speaks REST directly.

Everything is batched: the legacy script issued one SELECT + one INSERT + one
UPSERT per session (~30.000 requests per run on a full mac), which is why it
regularly failed halfway through.
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

logger = logging.getLogger(__name__)

# Status codes worth retrying (rate limit / transient backend trouble)
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3

# PostgREST resolves an unqualified request against the first exposed schema.
# Everything the uploads touch lives in `public`.
DEFAULT_SCHEMA = "public"


class SupabaseRestError(RuntimeError):
    """A PostgREST request failed."""


def _is_permission_error(error_text: str) -> bool:
    """True for "you may not write here" — a property of the table, not the row.

    Retrying such an error row by row is pointless: every row fails identically.
    """
    lowered = error_text.lower()
    return (
        "42501" in lowered
        or "permission denied" in lowered
        or "http 401" in lowered
        or "http 403" in lowered
    )


def _unknown_column(error_text: str, row_keys: set[str], table: str) -> str | None:
    """Extract the column name from a "column does not exist" error.

    PostgREST answers PGRST204 ("Could not find the 'x' column of 'y' in the
    schema cache"), PostgreSQL answers 42703 ("column y.x does not exist").
    Anything else is not a column problem.
    """
    lowered = error_text.lower()
    is_column_error = (
        "pgrst204" in lowered
        or "42703" in lowered
        or ("column" in lowered and ("does not exist" in lowered or "could not find" in lowered))
    )
    if not is_column_error:
        return None

    candidates = re.findall(r"'([^']+)'", error_text)
    candidates += [c.split(".")[-1] for c in re.findall(r"column ([\w.]+) does not exist", error_text)]

    # Prefer a candidate that is actually a key we sent
    for candidate in candidates:
        if candidate in row_keys:
            return candidate
    for candidate in candidates:
        if candidate != table:
            return candidate
    return None


class SupabaseRest:
    """Tiny PostgREST wrapper with batching and retry/backoff."""

    def __init__(
        self,
        url: str,
        key: str,
        timeout: int = DEFAULT_TIMEOUT,
        schema: str = DEFAULT_SCHEMA,
    ):
        self.root = (url or "").strip().rstrip("/")
        self.base = f"{self.root}/rest/v1"
        self.key = (key or "").strip()
        self.timeout = timeout
        self.schema = (schema or DEFAULT_SCHEMA).strip()
        # (Serverzeit, monotonic beim Empfang) — siehe server_now()
        self._server_time: tuple[datetime, float] | None = None

    @property
    def configured(self) -> bool:
        """True when both URL and key are set."""
        return bool(self.root and self.key)

    def _headers(self, extra: dict | None = None, method: str = "GET") -> dict:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

        # Without these, PostgREST uses whichever schema it exposes first. That
        # is not necessarily `public` — a project may expose a narrow read-only
        # `api` schema ahead of it, and writes would then hit the wrong relation.
        # supabase-py sends the same headers, so this matches the client the
        # legacy upload script used.
        if method in ("GET", "HEAD"):
            headers["Accept-Profile"] = self.schema
        else:
            headers["Content-Profile"] = self.schema

        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        payload=None,
        headers: dict | None = None,
        retries: int = MAX_RETRIES,
    ) -> requests.Response:
        """Perform a request, retrying transient failures with backoff."""
        last_error: Exception | None = None

        for attempt in range(1, retries + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(headers, method),
                    params=params,
                    data=json.dumps(payload) if payload is not None else None,
                    timeout=self.timeout,
                )
            except requests.RequestException as e:
                last_error = e
                if attempt >= retries:
                    break
                wait = 2**attempt
                logger.warning(
                    f"{method} {url} failed (attempt {attempt}/{retries}): {e}. "
                    f"Retrying in {wait}s"
                )
                time.sleep(wait)
                continue

            if response.status_code in RETRYABLE_STATUS and attempt < retries:
                wait = 2**attempt
                logger.warning(
                    f"{method} {url} → HTTP {response.status_code} "
                    f"(attempt {attempt}/{retries}). Retrying in {wait}s"
                )
                time.sleep(wait)
                continue

            self._note_server_time(response.headers.get("Date"))

            if response.status_code >= 400:
                raise SupabaseRestError(
                    f"HTTP {response.status_code}: {response.text[:400]}"
                )

            return response

        raise SupabaseRestError(f"Request failed after {retries} attempts: {last_error}")

    # ── Serverzeit ────────────────────────────────────────────────────

    def _note_server_time(self, date_header: str | None) -> None:
        """Zeit aus dem HTTP-Date-Header jeder Antwort merken."""
        if not date_header:
            return
        try:
            parsed = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            return
        if parsed is None:
            return
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        self._server_time = (parsed, time.monotonic())

    def server_now(self) -> datetime | None:
        """Aktuelle Zeit laut Datenbank-Server, oder None vor der ersten Antwort.

        Auf den Macs darf die Systemuhr bewusst verstellt sein. Jede Zeitrechnung,
        die zwischen Mac und Datenbank verglichen wird, muss deshalb eine gemeinsame
        Referenz benutzen — sonst verwirft ein vorgehender Mac jeden Auftrag als
        abgelaufen, bevor er ihn ausführt.

        Fortgeschrieben wird mit `time.monotonic()`: das läuft weiter, auch wenn
        jemand die Systemuhr mitten im Betrieb verstellt.
        """
        if self._server_time is None:
            return None
        base, received_at = self._server_time
        return base + timedelta(seconds=time.monotonic() - received_at)

    # ── Schema ────────────────────────────────────────────────────────

    def columns_with_source(self, table: str) -> tuple[set[str], str]:
        """Discover the columns of a table and say how reliable the answer is.

        Returns (columns, source):

        * ``openapi`` — read from the OpenAPI spec. Authoritative: this is the
          real table definition.
        * ``row`` — derived from the keys of one sampled row. **Not** a schema:
          a key may be absent because the role lacks column-level SELECT rights
          even though the column exists. Never use this to drop payload fields.
        * ``unknown`` — nothing worked.
        """
        try:
            response = self._request("GET", f"{self.base}/", retries=1)
            definitions = response.json().get("definitions", {})
            if table in definitions:
                cols = set(definitions[table].get("properties", {}).keys())
                if cols:
                    return cols, "openapi"
        except Exception as e:
            logger.debug(f"OpenAPI column discovery failed for '{table}': {e}")

        try:
            response = self._request(
                "GET", f"{self.base}/{table}", params={"select": "*", "limit": 1}, retries=1
            )
            rows = response.json()
            if rows:
                return set(rows[0].keys()), "row"
        except Exception as e:
            logger.debug(f"Row-based column discovery failed for '{table}': {e}")

        return set(), "unknown"

    def columns(self, table: str) -> set[str]:
        """Discover the columns of a table (see columns_with_source)."""
        return self.columns_with_source(table)[0]

    def count(self, table: str) -> int | None:
        """Return an estimated row count, or None when unavailable.

        Deliberately `count=planned`: an exact count on a table with millions of
        rows makes PostgREST time out with a 500.
        """
        try:
            response = self._request(
                "GET",
                f"{self.base}/{table}",
                params={"select": "*", "limit": 1},
                headers={"Prefer": "count=planned", "Range-Unit": "items", "Range": "0-0"},
                retries=1,
            )
            content_range = response.headers.get("content-range", "")
            if "/" in content_range:
                total = content_range.split("/")[-1]
                return int(total) if total.isdigit() else None
        except Exception as e:
            logger.debug(f"Count failed for '{table}': {e}")
        return None

    # ── Reads ─────────────────────────────────────────────────────────

    def select_existing(
        self, table: str, column: str, values: list[str], chunk_size: int = 100
    ) -> set[str]:
        """Return the subset of `values` that already exists in `table.column`.

        Replaces the legacy script's one-SELECT-per-session duplicate check.
        """
        found: set[str] = set()
        for i in range(0, len(values), chunk_size):
            chunk = [v for v in values[i : i + chunk_size] if v]
            if not chunk:
                continue
            quoted = ",".join('"' + str(v).replace('"', '\\"') + '"' for v in chunk)
            response = self._request(
                "GET",
                f"{self.base}/{table}",
                params={"select": column, column: f"in.({quoted})"},
            )
            for row in response.json():
                if row.get(column) is not None:
                    found.add(str(row[column]))
        return found

    def select(self, table: str, params: dict) -> list[dict]:
        """Run a raw select with PostgREST query parameters."""
        response = self._request("GET", f"{self.base}/{table}", params=params)
        return response.json()

    def update(
        self,
        table: str,
        params: dict,
        values: dict,
        *,
        retries: int = MAX_RETRIES,
    ) -> list[dict]:
        """PATCH rows matching `params` and return the rows actually changed.

        The returned list is what makes a conditional update usable as a lock:
        `PATCH ?id=eq.7&status=eq.queued` returns one row for whoever won and an
        empty list for everyone else.

        Pass ``retries=1`` for exactly that use. A retried PATCH would re-evaluate
        its own condition — by then already false — and report a loss that never
        happened.
        """
        response = self._request(
            "PATCH",
            f"{self.base}/{table}",
            params=params,
            payload=values,
            headers={"Prefer": "return=representation"},
            retries=retries,
        )
        try:
            rows = response.json()
        except ValueError:
            return []
        return rows if isinstance(rows, list) else [rows]

    def delete(self, table: str, params: dict) -> None:
        """DELETE rows matching `params` (PostgREST filters — never call without one)."""
        if not params:
            raise ValueError("delete ohne Filter verweigert")
        self._request("DELETE", f"{self.base}/{table}", params=params)

    # ── Writes ────────────────────────────────────────────────────────

    def _write_batch(
        self, table: str, rows: list[dict], params: dict, prefer: str
    ) -> None:
        self._request(
            "POST",
            f"{self.base}/{table}",
            params=params,
            payload=rows,
            headers={"Prefer": prefer},
        )

    def _write(
        self, table: str, rows: list[dict], params: dict, prefer: str, batch_size: int
    ) -> dict:
        """Write rows in batches, recovering from missing columns and bad rows.

        Columns are never dropped up front — a column can be invisible to the
        key while still existing (column-level SELECT rights), and dropping it
        would silently lose data. Instead a column is removed only when the
        server actually rejects it, and then for all remaining batches too.

        If a batch still fails, it is retried row by row so one bad row does not
        take the other 99 with it.
        """
        written = 0
        errors: list[str] = []
        removed: set[str] = set()

        def strip(batch: list[dict]) -> list[dict]:
            if not removed:
                return batch
            return [{k: v for k, v in row.items() if k not in removed} for row in batch]

        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]

            while True:
                current = strip(batch)
                try:
                    self._write_batch(table, current, params, prefer)
                    written += len(current)
                    break
                except Exception as e:
                    if _is_permission_error(str(e)):
                        # Missing write rights apply to the whole table. The old
                        # code retried all 946 rows individually to learn this.
                        logger.error(
                            f"Keine Schreibrechte auf '{table}' — Upload abgebrochen: {e}"
                        )
                        return {
                            "written": written,
                            "failed": len(rows) - written,
                            "errors": [str(e)[:300]],
                            "aborted": "no_write_permission",
                            "removed_columns": sorted(removed),
                        }

                    bad_column = _unknown_column(
                        str(e), set(current[0]) if current else set(), table
                    )
                    if bad_column and bad_column not in removed and len(removed) < 30:
                        removed.add(bad_column)
                        logger.warning(
                            f"'{table}' hat keine Spalte '{bad_column}' — "
                            f"wird für diesen Upload weggelassen"
                        )
                        continue

                    logger.warning(
                        f"Batch write to '{table}' failed ({len(current)} rows), "
                        f"retrying row by row: {e}"
                    )
                    for row in current:
                        try:
                            self._write_batch(table, [row], params, prefer)
                            written += 1
                        except Exception as row_error:
                            if _is_permission_error(str(row_error)):
                                logger.error(
                                    f"Keine Schreibrechte auf '{table}' — Upload abgebrochen: "
                                    f"{row_error}"
                                )
                                return {
                                    "written": written,
                                    "failed": len(rows) - written,
                                    "errors": [str(row_error)[:300]],
                                    "aborted": "no_write_permission",
                                    "removed_columns": sorted(removed),
                                }
                            message = str(row_error)[:200]
                            errors.append(message)
                            logger.error(f"Row write to '{table}' failed: {message}")
                    break

        result = {"written": written, "failed": len(errors), "errors": errors[:10]}
        if removed:
            result["removed_columns"] = sorted(removed)
        return result

    def insert_many(self, table: str, rows: list[dict], batch_size: int = 100) -> dict:
        """Plain INSERT (no conflict handling)."""
        if not rows:
            return {"written": 0, "failed": 0, "errors": []}
        return self._write(table, rows, {}, "return=minimal", batch_size)

    def upsert_many(
        self, table: str, rows: list[dict], on_conflict: str, batch_size: int = 200
    ) -> dict:
        """UPSERT with conflict resolution on `on_conflict`."""
        if not rows:
            return {"written": 0, "failed": 0, "errors": []}
        return self._write(
            table,
            rows,
            {"on_conflict": on_conflict},
            "resolution=merge-duplicates,return=minimal",
            batch_size,
        )


# ── Connection test ───────────────────────────────────────────────────

# Adding columns needs DDL, which PostgREST cannot do with any key. So instead of
# creating them we report what is missing and hand out ready-made SQL.
MAX_SQL_COLUMNS = 40


def build_alter_sql(table: str, columns: dict[str, str]) -> str:
    """Build an ALTER TABLE statement for the missing columns."""
    if not columns:
        return ""

    items = list(columns.items())
    truncated = len(items) > MAX_SQL_COLUMNS
    items = items[:MAX_SQL_COLUMNS]

    additions = ",\n".join(
        f'  ADD COLUMN IF NOT EXISTS "{name}" {sql_type}' for name, sql_type in items
    )
    statement = f'ALTER TABLE "{table}"\n{additions};'
    if truncated:
        statement += f"\n-- gekürzt: nur die ersten {MAX_SQL_COLUMNS} Spalten"
    return statement


def describe_target(
    url: str,
    key: str,
    table: str,
    expected: dict[str, str] | None = None,
    unique_column: str | None = None,
    schema: str = DEFAULT_SCHEMA,
) -> dict:
    """Check whether a target table is reachable and which columns are missing."""
    if not table:
        return {"status": "error", "error": "Kein Tabellenname gesetzt"}

    api = SupabaseRest(url, key, schema=schema)
    if not api.configured:
        return {
            "status": "not_configured",
            "table": table,
            "error": "URL oder Key fehlt",
        }

    columns, source = api.columns_with_source(table)
    if not columns:
        return {
            "status": "error",
            "table": table,
            "error": (
                f"Tabelle '{table}' nicht erreichbar — URL, Key und Tabellenname prüfen"
            ),
        }

    result = {
        "status": "ok",
        "table": table,
        "schema": api.schema,
        "columns": len(columns),
        "rows": api.count(table),
        "missing": [],
        "verified": source == "openapi",
    }

    if expected:
        missing = {name: t for name, t in expected.items() if name not in columns}
        result["missing"] = list(missing)
        if missing:
            # Only the OpenAPI spec is a real schema. A sampled row just shows
            # what this key may read — a column can exist and still be absent
            # there, so we must not call it missing.
            result["status"] = "incomplete" if source == "openapi" else "unverified"
            result["sql"] = build_alter_sql(table, missing)

    if unique_column:
        result["hint"] = (
            f"Für das UPSERT braucht '{table}' einen UNIQUE-Index auf "
            f"'{unique_column}':\nCREATE UNIQUE INDEX IF NOT EXISTS "
            f'{table}_{unique_column}_key ON "{table}" ("{unique_column}");'
        )

    return result
