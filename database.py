"""SQLite schema, connection configuration, and import-state operations."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import logging
import sqlite3
import time
from typing import Callable, Iterator, Sequence

from config import Settings

SCHEMA_VERSION = "5"
LOGGER = logging.getLogger("telegram_csv_search")
SQLITE_WRITE_TIMEOUT_SECONDS = 120
SQLITE_BUSY_TIMEOUT_MS = 120_000
INDEX_BUILD_RETRIES = 4
INDEX_RETRY_DELAYS_SECONDS = (1, 2, 4)


class DatabaseRebuildRequired(RuntimeError):
    """Raised when an old database cannot safely serve offset-backed records."""


@dataclass(frozen=True, slots=True)
class ImportedFile:
    id: int
    relative_path: str
    file_size: int
    modified_time_ns: int
    status: str
    rows_imported: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(database_path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Open a short-lived SQLite connection configured for concurrent readers."""
    if readonly:
        uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=15)
    else:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path, timeout=SQLITE_WRITE_TIMEOUT_SECONDS)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    if not readonly:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
        # A bounded page cache protects VPS RAM; SQLite still reads from disk on demand.
        connection.execute("PRAGMA cache_size = -65536")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize_database(connection: sqlite3.Connection) -> None:
    """Create the offset-backed schema or reject an incompatible legacy database.

    Version 1 stored complete source rows in ``records.raw_fields_json`` and did not
    have byte offsets. There is no safe way to infer exact byte ranges from that data,
    especially for quoted multiline CSV records, so those databases must be rebuilt
    from their source CSV files.
    """
    existing_records = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
    metadata_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
    ).fetchone()
    stored_version = None
    if metadata_exists:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        stored_version = str(row[0]) if row else None
    required_offset_columns = {"byte_offset_start", "byte_offset_end"}
    if existing_records and (
        "raw_fields_json" in existing_records
        or not required_offset_columns.issubset(existing_records)
    ):
        raise DatabaseRebuildRequired(
            "Legacy SQLite schema detected. This version reads result rows directly "
            "from CSV byte offsets, so the existing database cannot be migrated safely. "
            "Keep every source CSV in DATA_DIRECTORY, stop the bot, remove the old "
            "search.db (and its -wal/-shm files), then run `python import_csv.py` to rebuild it."
        )
    if stored_version not in {None, "2", "3", "4", SCHEMA_VERSION}:
        raise DatabaseRebuildRequired(
            f"Unsupported SQLite schema version {stored_version!r}; expected "
            f"2, 3, 4 or {SCHEMA_VERSION}. Rebuild the database from the CSV files in DATA_DIRECTORY."
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS imported_files (
            id INTEGER PRIMARY KEY,
            relative_path TEXT NOT NULL UNIQUE,
            headers_json TEXT NOT NULL DEFAULT '[]',
            column_names_json TEXT NOT NULL DEFAULT '[]',
            selected_columns_json TEXT NOT NULL DEFAULT '[]',
            csv_encoding TEXT NOT NULL,
            csv_delimiter TEXT NOT NULL,
            has_header INTEGER NOT NULL DEFAULT 0 CHECK (has_header IN (0, 1)),
            file_size INTEGER NOT NULL,
            modified_time_ns INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('importing', 'completed', 'failed')),
            rows_imported INTEGER NOT NULL DEFAULT 0,
            last_line INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT,
            telegram_file_id TEXT,
            telegram_file_unique_id TEXT,
            telegram_filename TEXT,
            telegram_file_size INTEGER,
            telegram_uploader_id INTEGER,
            telegram_chat_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY,
            source_file_id INTEGER NOT NULL REFERENCES imported_files(id) ON DELETE CASCADE,
            source_line INTEGER NOT NULL,
            byte_offset_start INTEGER NOT NULL CHECK (byte_offset_start >= 0),
            byte_offset_end INTEGER NOT NULL CHECK (byte_offset_end > byte_offset_start),
            column_1_norm TEXT NOT NULL DEFAULT '',
            column_2_norm TEXT NOT NULL DEFAULT '',
            name_norm TEXT NOT NULL DEFAULT '',
            birth_date_norm TEXT NOT NULL DEFAULT '',
            phone_norm TEXT NOT NULL DEFAULT '',
            search_text_norm TEXT NOT NULL DEFAULT '',
            record_hash TEXT NOT NULL,
            UNIQUE(source_file_id, byte_offset_start)
        );

        CREATE TABLE IF NOT EXISTS dedupe_hashes (
            record_hash TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS bot_users (
            user_id INTEGER PRIMARY KEY CHECK (user_id > 0),
            added_by INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS header_aliases (
            header_key TEXT PRIMARY KEY,
            source_header TEXT NOT NULL,
            display_header TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE TABLE IF NOT EXISTS hidden_headers (
            header_key TEXT PRIMARY KEY,
            source_header TEXT NOT NULL,
            updated_at TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_records_source_file ON records(source_file_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS records_all_fts USING fts5(
            search_text_norm,
            content='records',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2',
            prefix='2 3 4 5 6 8'
        );
        """
    )
    imported_columns = {row[1] for row in connection.execute("PRAGMA table_info(imported_files)")}
    if "selected_columns_json" not in imported_columns:
        connection.execute(
            "ALTER TABLE imported_files ADD COLUMN selected_columns_json TEXT NOT NULL DEFAULT '[]'"
        )
        # Version 2 indexed every source column. Preserve that behavior when migrating
        # existing data; new imports can narrow their indexed columns explicitly.
        import json
        for row in connection.execute("SELECT id, column_names_json FROM imported_files").fetchall():
            try:
                names = json.loads(row[1] or "[]")
                indexes = list(range(len(names))) if isinstance(names, list) else []
            except (TypeError, ValueError):
                indexes = []
            connection.execute(
                "UPDATE imported_files SET selected_columns_json = ? WHERE id = ?",
                (json.dumps(indexes), row[0]),
            )
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SCHEMA_VERSION,),
    )
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES ('search_indexes_ready', '0')"
    )
    connection.commit()


def get_metadata(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else default


def set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO metadata(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def initialize_access_control(connection: sqlite3.Connection, settings: Settings) -> None:
    """Seed private-mode users once; later changes made by admins remain persistent."""
    mode = get_metadata(connection, "access_mode")
    if mode not in {"private", "public"}:
        set_metadata(connection, "access_mode", settings.initial_access_mode)
    if get_metadata(connection, "access_bootstrap_done") != "1":
        for user_id in sorted(settings.allowed_user_ids | settings.admin_user_ids):
            connection.execute(
                "INSERT OR IGNORE INTO bot_users(user_id, added_by, created_at) VALUES (?, ?, ?)",
                (user_id, 0, utc_now()),
            )
        set_metadata(connection, "access_bootstrap_done", "1")
    connection.commit()


def access_mode(connection: sqlite3.Connection) -> str:
    value = get_metadata(connection, "access_mode", "private")
    return value if value in {"private", "public"} else "private"


def set_access_mode(connection: sqlite3.Connection, mode: str) -> None:
    if mode not in {"private", "public"}:
        raise ValueError("Access mode must be private or public.")
    set_metadata(connection, "access_mode", mode)
    connection.commit()


def list_bot_users(connection: sqlite3.Connection) -> list[int]:
    return [int(row[0]) for row in connection.execute("SELECT user_id FROM bot_users ORDER BY user_id")]


def bot_user_exists(connection: sqlite3.Connection, user_id: int) -> bool:
    return connection.execute("SELECT 1 FROM bot_users WHERE user_id = ?", (int(user_id),)).fetchone() is not None


def add_bot_user(connection: sqlite3.Connection, user_id: int, added_by: int) -> bool:
    if int(user_id) <= 0:
        raise ValueError("Telegram user ID must be positive.")
    cursor = connection.execute(
        "INSERT OR IGNORE INTO bot_users(user_id, added_by, created_at) VALUES (?, ?, ?)",
        (int(user_id), int(added_by), utc_now()),
    )
    connection.commit()
    return cursor.rowcount > 0


def remove_bot_user(connection: sqlite3.Connection, user_id: int) -> bool:
    cursor = connection.execute("DELETE FROM bot_users WHERE user_id = ?", (int(user_id),))
    connection.commit()
    return cursor.rowcount > 0


def header_alias_key(header: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKC", str(header)).strip().casefold()


def get_header_aliases(connection: sqlite3.Connection) -> dict[str, str]:
    return {str(row[0]): str(row[1]) for row in connection.execute(
        "SELECT header_key, display_header FROM header_aliases ORDER BY header_key"
    )}


def list_header_alias_entries(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            "SELECT source_header, display_header FROM header_aliases ORDER BY source_header COLLATE NOCASE"
        )
    ]


def set_header_alias(connection: sqlite3.Connection, source_header: str, display_header: str) -> None:
    key = header_alias_key(source_header)
    source = str(source_header).strip()
    display = str(display_header).strip()
    if not key or not source or not display:
        raise ValueError("Both source and display header names are required.")
    connection.execute(
        "INSERT INTO header_aliases(header_key, source_header, display_header, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(header_key) DO UPDATE SET "
        "source_header=excluded.source_header, display_header=excluded.display_header, updated_at=excluded.updated_at",
        (key, source, display, utc_now()),
    )
    connection.commit()


def delete_header_alias(connection: sqlite3.Connection, source_header: str) -> bool:
    cursor = connection.execute("DELETE FROM header_aliases WHERE header_key = ?", (header_alias_key(source_header),))
    connection.commit()
    return cursor.rowcount > 0


def get_hidden_header_keys(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT header_key FROM hidden_headers")
    }


def list_hidden_headers(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT source_header FROM hidden_headers ORDER BY source_header COLLATE NOCASE"
        )
    ]


def hide_header(connection: sqlite3.Connection, source_header: str) -> bool:
    source = str(source_header).strip()
    key = header_alias_key(source)
    if not key or not source:
        raise ValueError("A source header name is required.")
    cursor = connection.execute(
        "INSERT OR IGNORE INTO hidden_headers(header_key, source_header, updated_at) VALUES (?, ?, ?)",
        (key, source, utc_now()),
    )
    connection.commit()
    return cursor.rowcount > 0


def show_header(connection: sqlite3.Connection, source_header: str) -> bool:
    cursor = connection.execute(
        "DELETE FROM hidden_headers WHERE header_key = ?",
        (header_alias_key(source_header),),
    )
    connection.commit()
    return cursor.rowcount > 0


def search_indexes_ready(connection: sqlite3.Connection) -> bool:
    return get_metadata(connection, "search_indexes_ready") == "1"


def build_search_indexes(
    connection: sqlite3.Connection,
    *,
    force: bool = False,
    progress_callback: Callable[[str, float], None] | None = None,
) -> None:
    """Build exact/prefix B-tree indexes and rebuild FTS after an initial bulk import."""
    original_progress_callback = progress_callback

    def progress_callback(stage: str, percent: float) -> None:
        if original_progress_callback is None:
            return
        try:
            original_progress_callback(stage, percent)
        except Exception:
            LOGGER.exception("Index progress callback failed")

    if search_indexes_ready(connection) and not force:
        if progress_callback:
            progress_callback("شاخص‌ها از قبل آماده‌اند", 100.0)
        return
    index_statements = (
        "CREATE INDEX IF NOT EXISTS idx_records_phone_norm ON records(phone_norm)",
        "CREATE INDEX IF NOT EXISTS idx_records_column_1_norm ON records(column_1_norm)",
        "CREATE INDEX IF NOT EXISTS idx_records_column_2_norm ON records(column_2_norm)",
        "CREATE INDEX IF NOT EXISTS idx_records_name_norm ON records(name_norm)",
        "CREATE INDEX IF NOT EXISTS idx_records_birth_date_norm ON records(birth_date_norm)",
        "CREATE INDEX IF NOT EXISTS idx_records_record_hash ON records(record_hash)",
    )
    for attempt in range(INDEX_BUILD_RETRIES):
        try:
            if progress_callback:
                progress_callback("انتظار برای آزادشدن پایگاه‌داده", 0.0)
            # Acquire SQLite's single-writer slot before doing the expensive FTS rebuild.
            # Re-check after acquiring it so concurrent initializers do not rebuild twice.
            connection.execute("BEGIN IMMEDIATE")
            if search_indexes_ready(connection) and not force:
                connection.commit()
                if progress_callback:
                    progress_callback("شاخص‌ها توسط فرایند دیگری آماده شدند", 100.0)
                return
            if progress_callback:
                progress_callback("قفل نوشتن گرفته شد؛ آماده‌سازی FTS", 5.0)
            record_count = int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
            changes_before_rebuild = connection.total_changes
            last_fts_percent = 5.0

            def report_fts_progress() -> int:
                nonlocal last_fts_percent
                if progress_callback and record_count:
                    changed = max(0, connection.total_changes - changes_before_rebuild)
                    # SQLite's total_changes is a useful approximation for FTS5 rebuild work.
                    # Clamp it below completion; the statement's return is the authoritative end.
                    percent = min(50.0, 5.0 + 45.0 * changed / record_count)
                    if percent >= last_fts_percent + 2.0:
                        last_fts_percent = percent
                        progress_callback("بازسازی شاخص متنی FTS", percent)
                return 0

            if progress_callback:
                progress_callback("بازسازی شاخص متنی FTS", 5.0)
            connection.set_progress_handler(report_fts_progress, 20_000)
            try:
                connection.execute("INSERT INTO records_all_fts(records_all_fts) VALUES ('rebuild')")
            finally:
                connection.set_progress_handler(None, 0)
            if progress_callback:
                progress_callback("بازسازی شاخص متنی FTS کامل شد", 52.0)
            for index_number, statement in enumerate(index_statements):
                index_name = statement.split()[5]
                start_percent = 52.0 + index_number * 6.0
                if progress_callback:
                    progress_callback(f"ساخت شاخص {index_name}", start_percent)
                connection.execute(statement)
                if progress_callback:
                    progress_callback(f"شاخص {index_name} ساخته شد", start_percent + 6.0)
            if force:
                if progress_callback:
                    progress_callback("بازسازی نهایی شاخص‌های B-tree", 90.0)
                connection.execute("REINDEX")
                if progress_callback:
                    progress_callback("بازسازی نهایی B-tree کامل شد", 97.0)
            set_metadata(connection, "search_indexes_ready", "1")
            connection.commit()
            if progress_callback:
                progress_callback("ساخت همهٔ شاخص‌ها کامل شد", 100.0)
            return
        except sqlite3.OperationalError as exc:
            connection.rollback()
            message = str(exc).lower()
            is_lock_error = "locked" in message or "busy" in message
            if not is_lock_error or attempt + 1 >= INDEX_BUILD_RETRIES:
                raise
            delay = INDEX_RETRY_DELAYS_SECONDS[min(attempt, len(INDEX_RETRY_DELAYS_SECONDS) - 1)]
            LOGGER.warning(
                "SQLite remained busy while building search indexes; retry=%d/%d delay_seconds=%d",
                attempt + 1, INDEX_BUILD_RETRIES, delay,
            )
            if progress_callback:
                progress_callback(
                    f"قفل موقت SQLite؛ تلاش دوباره {attempt + 2} از {INDEX_BUILD_RETRIES}",
                    0.0,
                )
            time.sleep(delay)


def get_imported_file(connection: sqlite3.Connection, relative_path: str) -> ImportedFile | None:
    row = connection.execute(
        "SELECT id, relative_path, file_size, modified_time_ns, status, rows_imported "
        "FROM imported_files WHERE relative_path = ?",
        (relative_path,),
    ).fetchone()
    return ImportedFile(**dict(row)) if row else None


def count_completed_files(connection: sqlite3.Connection) -> int:
    return int(connection.execute(
        "SELECT COUNT(*) FROM imported_files WHERE status = 'completed'"
    ).fetchone()[0])


def list_completed_files(
    connection: sqlite3.Connection, *, limit: int, offset: int
) -> list[sqlite3.Row]:
    return list(connection.execute(
        """SELECT f.id, f.relative_path, f.file_size, f.status, f.rows_imported,
                  f.completed_at, f.telegram_filename, COUNT(r.id) AS record_count
           FROM imported_files f
           LEFT JOIN records r ON r.source_file_id = f.id
           WHERE f.status = 'completed'
           GROUP BY f.id
           ORDER BY COALESCE(f.completed_at, f.started_at) DESC, f.id DESC
           LIMIT ? OFFSET ?""",
        (max(1, int(limit)), max(0, int(offset))),
    ).fetchall())


def get_imported_file_details(
    connection: sqlite3.Connection, file_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        """SELECT f.id, f.relative_path, f.file_size, f.status, f.rows_imported,
                  f.completed_at, f.telegram_filename, COUNT(r.id) AS record_count
           FROM imported_files f
           LEFT JOIN records r ON r.source_file_id = f.id
           WHERE f.id = ?
           GROUP BY f.id""",
        (int(file_id),),
    ).fetchone()


def delete_imported_file(
    connection: sqlite3.Connection, file_id: int
) -> dict[str, object] | None:
    """Remove one completed file, its records, FTS entries, and unused dedupe hashes."""
    details = get_imported_file_details(connection, file_id)
    if details is None:
        return None
    if str(details["status"]) != "completed":
        raise ValueError("Only completed imports can be removed from the database.")

    source_id = int(details["id"])
    deleted_rows = int(details["record_count"])
    with transaction(connection):
        connection.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _deleted_file_hashes(record_hash TEXT PRIMARY KEY)"
        )
        connection.execute("DELETE FROM _deleted_file_hashes")
        connection.execute(
            "INSERT OR IGNORE INTO _deleted_file_hashes "
            "SELECT DISTINCT record_hash FROM records WHERE source_file_id = ?",
            (source_id,),
        )
        if deleted_rows and search_indexes_ready(connection):
            connection.execute(
                """INSERT INTO records_all_fts(records_all_fts, rowid, search_text_norm)
                   SELECT 'delete', id, search_text_norm FROM records WHERE source_file_id = ?""",
                (source_id,),
            )
        connection.execute("DELETE FROM records WHERE source_file_id = ?", (source_id,))
        connection.execute("DELETE FROM imported_files WHERE id = ?", (source_id,))
        connection.execute(
            """DELETE FROM dedupe_hashes
               WHERE record_hash IN (SELECT record_hash FROM _deleted_file_hashes)
                 AND NOT EXISTS (
                     SELECT 1 FROM records r WHERE r.record_hash = dedupe_hashes.record_hash
                 )"""
        )

    return {
        "id": source_id,
        "relative_path": str(details["relative_path"]),
        "file_size": int(details["file_size"]),
        "record_count": deleted_rows,
        "telegram_filename": details["telegram_filename"],
    }


def file_needs_import(
    existing: ImportedFile | None,
    *,
    file_size: int,
    modified_time_ns: int,
    force: bool,
) -> bool:
    if force or existing is None:
        return True
    if existing.status != "completed":
        return True
    return existing.file_size != file_size or existing.modified_time_ns != modified_time_ns


def mark_file_importing(
    connection: sqlite3.Connection,
    *,
    relative_path: str,
    file_size: int,
    modified_time_ns: int,
    telegram_metadata: dict[str, object] | None = None,
    headers_json: str = "[]",
    column_names_json: str = "[]",
    csv_encoding: str = "utf-8-sig",
    csv_delimiter: str = ",",
) -> int:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO imported_files (
            relative_path, headers_json, column_names_json, csv_encoding, csv_delimiter,
            file_size, modified_time_ns, status, rows_imported, last_line, started_at,
            completed_at, error_message
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'importing', 0, 0, ?, NULL, NULL)
        ON CONFLICT(relative_path) DO UPDATE SET
            headers_json = excluded.headers_json,
            column_names_json = excluded.column_names_json,
            csv_encoding = excluded.csv_encoding,
            csv_delimiter = excluded.csv_delimiter,
            has_header = 0,
            file_size = excluded.file_size,
            modified_time_ns = excluded.modified_time_ns,
            status = 'importing',
            rows_imported = 0,
            last_line = 0,
            started_at = excluded.started_at,
            completed_at = NULL,
            error_message = NULL
        """,
        (
            relative_path,
            headers_json,
            column_names_json,
            csv_encoding,
            csv_delimiter,
            file_size,
            modified_time_ns,
            now,
        ),
    )
    row = connection.execute(
        "SELECT id FROM imported_files WHERE relative_path = ?", (relative_path,)
    ).fetchone()
    if telegram_metadata:
        connection.execute(
            """UPDATE imported_files SET telegram_file_id = ?, telegram_file_unique_id = ?,
               telegram_filename = ?, telegram_file_size = ?, telegram_uploader_id = ?, telegram_chat_id = ?
               WHERE relative_path = ?""",
            (
                telegram_metadata.get("file_id"), telegram_metadata.get("file_unique_id"),
                telegram_metadata.get("filename"), telegram_metadata.get("file_size"),
                telegram_metadata.get("uploader_id"), telegram_metadata.get("chat_id"), relative_path,
            ),
        )
    connection.commit()
    return int(row[0])


def update_file_csv_metadata(
    connection: sqlite3.Connection,
    file_id: int,
    *,
    headers_json: str,
    column_names_json: str,
    selected_columns_json: str,
    has_header: bool,
) -> None:
    """Persist the parser and search-selection metadata for source hydration."""
    connection.execute(
        """UPDATE imported_files
           SET headers_json = ?, column_names_json = ?, selected_columns_json = ?, has_header = ?
           WHERE id = ?""",
        (headers_json, column_names_json, selected_columns_json, int(has_header), file_id),
    )
    connection.commit()


def update_import_progress(
    connection: sqlite3.Connection, file_id: int, rows_imported: int, last_line: int
) -> None:
    connection.execute(
        "UPDATE imported_files SET rows_imported = ?, last_line = ? WHERE id = ?",
        (rows_imported, last_line, file_id),
    )


def mark_file_completed(connection: sqlite3.Connection, file_id: int, rows_imported: int) -> None:
    connection.execute(
        """UPDATE imported_files
           SET status = 'completed', rows_imported = ?, completed_at = ?, error_message = NULL
           WHERE id = ?""",
        (rows_imported, utc_now(), file_id),
    )
    connection.commit()


def mark_file_failed(connection: sqlite3.Connection, file_id: int, error_message: str) -> None:
    connection.execute(
        "UPDATE imported_files SET status = 'failed', error_message = ? WHERE id = ?",
        (error_message[:1000], file_id),
    )
    connection.commit()


def remove_records_for_source(connection: sqlite3.Connection, source_file_id: int) -> int:
    """Remove a partial/changed file and keep FTS and dedupe state consistent."""
    count = int(
        connection.execute(
            "SELECT COUNT(*) FROM records WHERE source_file_id = ?", (source_file_id,)
        ).fetchone()[0]
    )
    if count == 0:
        return 0

    connection.execute("CREATE TEMP TABLE IF NOT EXISTS _removed_hashes(record_hash TEXT PRIMARY KEY)")
    connection.execute("DELETE FROM _removed_hashes")
    connection.execute(
        "INSERT OR IGNORE INTO _removed_hashes SELECT DISTINCT record_hash FROM records "
        "WHERE source_file_id = ?",
        (source_file_id,),
    )
    if search_indexes_ready(connection):
        connection.execute(
            """INSERT INTO records_all_fts(records_all_fts, rowid, search_text_norm)
               SELECT 'delete', id, search_text_norm FROM records WHERE source_file_id = ?""",
            (source_file_id,),
        )
    connection.execute("DELETE FROM records WHERE source_file_id = ?", (source_file_id,))
    connection.execute(
        """DELETE FROM dedupe_hashes
           WHERE record_hash IN (SELECT record_hash FROM _removed_hashes)
             AND NOT EXISTS (
                 SELECT 1 FROM records r WHERE r.record_hash = dedupe_hashes.record_hash
             )"""
    )
    connection.commit()
    return count


def seed_dedupe_hashes(connection: sqlite3.Connection) -> None:
    """Seed duplicate state when exact-deduplication is enabled after prior imports."""
    connection.execute(
        "INSERT OR IGNORE INTO dedupe_hashes(record_hash, created_at) "
        "SELECT record_hash, ? FROM records",
        (utc_now(),),
    )
    connection.commit()


def insert_record_batch(
    connection: sqlite3.Connection,
    rows: Sequence[tuple],
    *,
    use_deduplication: bool,
    update_fts: bool,
) -> int:
    """Insert a bounded batch and return records actually retained."""
    if not rows:
        return 0
    retained: list[tuple] = []
    with transaction(connection):
        if use_deduplication:
            for row in rows:
                record_hash = row[-1]
                result = connection.execute(
                    "INSERT OR IGNORE INTO dedupe_hashes(record_hash, created_at) VALUES (?, ?)",
                    (record_hash, utc_now()),
                )
                if result.rowcount:
                    retained.append(row)
        else:
            retained = list(rows)

        if retained:
            connection.executemany(
                """INSERT INTO records(
                    source_file_id, source_line, byte_offset_start, byte_offset_end,
                    column_1_norm, column_2_norm, name_norm, birth_date_norm, phone_norm,
                    search_text_norm, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                retained,
            )
            # sqlite3.executemany() does not reliably expose all generated IDs. Retrieve by
            # the file/start-offset pairs after the insert instead.
            if update_fts:
                for row in retained:
                    source_id, byte_offset_start = row[0], row[2]
                    record = connection.execute(
                        "SELECT id, search_text_norm FROM records "
                        "WHERE source_file_id = ? AND byte_offset_start = ?",
                        (source_id, byte_offset_start),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO records_all_fts(rowid, search_text_norm) VALUES (?, ?)",
                        (record["id"], record["search_text_norm"]),
                    )
    return len(retained)


def database_statistics(connection: sqlite3.Connection, database_path: Path) -> dict[str, object]:
    records = int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
    completed = int(
        connection.execute("SELECT COUNT(*) FROM imported_files WHERE status = 'completed'").fetchone()[0]
    )
    importing = int(
        connection.execute("SELECT COUNT(*) FROM imported_files WHERE status = 'importing'").fetchone()[0]
    )
    failed = int(
        connection.execute("SELECT COUNT(*) FROM imported_files WHERE status = 'failed'").fetchone()[0]
    )
    last_import = connection.execute(
        "SELECT MAX(completed_at) FROM imported_files WHERE status = 'completed'"
    ).fetchone()[0]
    return {
        "records": records,
        "completed_files": completed,
        "importing_files": importing,
        "failed_files": failed,
        "last_import": last_import or "Never",
        "database_bytes": database_path.stat().st_size if database_path.exists() else 0,
        "search_indexes_ready": search_indexes_ready(connection),
    }
