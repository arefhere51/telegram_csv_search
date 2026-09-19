"""SQLite schema, connection configuration, and import-state operations."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator, Sequence

from config import Settings

SCHEMA_VERSION = "1"


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
        connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
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
    """Create durable tables. Search indexes are built after first import."""
    # Add columns before creating content-backed FTS tables so an existing
    # installation can be upgraded without dropping its records.
    existing_records = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
    if existing_records and "search_text_norm" not in existing_records:
        connection.execute("ALTER TABLE records ADD COLUMN search_text_norm TEXT NOT NULL DEFAULT ''")
    existing_files = {row[1] for row in connection.execute("PRAGMA table_info(imported_files)")}
    if existing_files and "headers_json" not in existing_files:
        connection.execute("ALTER TABLE imported_files ADD COLUMN headers_json TEXT NOT NULL DEFAULT '[]'")
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
            file_size INTEGER NOT NULL,
            modified_time_ns INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('importing', 'completed', 'failed')),
            rows_imported INTEGER NOT NULL DEFAULT 0,
            last_line INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY,
            source_file_id INTEGER NOT NULL REFERENCES imported_files(id) ON DELETE CASCADE,
            source_line INTEGER NOT NULL,
            column_1 TEXT NOT NULL DEFAULT '',
            column_2 TEXT NOT NULL DEFAULT '',
            name TEXT NOT NULL DEFAULT '',
            birth_date TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            column_1_norm TEXT NOT NULL DEFAULT '',
            column_2_norm TEXT NOT NULL DEFAULT '',
            name_norm TEXT NOT NULL DEFAULT '',
            birth_date_norm TEXT NOT NULL DEFAULT '',
            phone_norm TEXT NOT NULL DEFAULT '',
            raw_fields_json TEXT NOT NULL DEFAULT '[]',
            search_text_norm TEXT NOT NULL DEFAULT '',
            record_hash TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dedupe_hashes (
            record_hash TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        ) WITHOUT ROWID;

        CREATE INDEX IF NOT EXISTS idx_records_source_file ON records(source_file_id);

        CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
            name_norm,
            content='records',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2',
            prefix='2 3 4 5 6 8'
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS records_all_fts USING fts5(
            search_text_norm,
            content='records',
            content_rowid='id',
            tokenize='unicode61 remove_diacritics 2',
            prefix='2 3 4 5 6 8'
        );
        """
    )
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(imported_files)")}
    migrations = {
        "headers_json": "ALTER TABLE imported_files ADD COLUMN headers_json TEXT NOT NULL DEFAULT '[]'",
        "telegram_file_id": "ALTER TABLE imported_files ADD COLUMN telegram_file_id TEXT",
        "telegram_file_unique_id": "ALTER TABLE imported_files ADD COLUMN telegram_file_unique_id TEXT",
        "telegram_filename": "ALTER TABLE imported_files ADD COLUMN telegram_filename TEXT",
        "telegram_file_size": "ALTER TABLE imported_files ADD COLUMN telegram_file_size INTEGER",
        "telegram_uploader_id": "ALTER TABLE imported_files ADD COLUMN telegram_uploader_id INTEGER",
        "telegram_chat_id": "ALTER TABLE imported_files ADD COLUMN telegram_chat_id INTEGER",
    }
    for column, statement in migrations.items():
        if column not in existing_columns:
            connection.execute(statement)
    connection.execute(
        "INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', ?)",
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


def search_indexes_ready(connection: sqlite3.Connection) -> bool:
    return get_metadata(connection, "search_indexes_ready") == "1"


def build_search_indexes(connection: sqlite3.Connection, *, force: bool = False) -> None:
    """Build exact/prefix B-tree indexes and rebuild FTS after an initial bulk import."""
    if search_indexes_ready(connection) and not force:
        return
    connection.execute("INSERT INTO records_fts(records_fts) VALUES ('rebuild')")
    connection.execute("INSERT INTO records_all_fts(records_all_fts) VALUES ('rebuild')")
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_records_phone_norm ON records(phone_norm);
        CREATE INDEX IF NOT EXISTS idx_records_column_1_norm ON records(column_1_norm);
        CREATE INDEX IF NOT EXISTS idx_records_column_2_norm ON records(column_2_norm);
        CREATE INDEX IF NOT EXISTS idx_records_name_norm ON records(name_norm);
        CREATE INDEX IF NOT EXISTS idx_records_birth_date_norm ON records(birth_date_norm);
        CREATE INDEX IF NOT EXISTS idx_records_record_hash ON records(record_hash);
        """
    )
    if force:
        connection.execute("REINDEX")
    set_metadata(connection, "search_indexes_ready", "1")
    connection.commit()


def get_imported_file(connection: sqlite3.Connection, relative_path: str) -> ImportedFile | None:
    row = connection.execute(
        "SELECT id, relative_path, file_size, modified_time_ns, status, rows_imported "
        "FROM imported_files WHERE relative_path = ?",
        (relative_path,),
    ).fetchone()
    return ImportedFile(**dict(row)) if row else None


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
) -> int:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO imported_files (
            relative_path, headers_json, file_size, modified_time_ns, status, rows_imported,
            last_line, started_at, completed_at, error_message
        ) VALUES (?, ?, ?, ?, 'importing', 0, 0, ?, NULL, NULL)
        ON CONFLICT(relative_path) DO UPDATE SET
            headers_json = excluded.headers_json,
            file_size = excluded.file_size,
            modified_time_ns = excluded.modified_time_ns,
            status = 'importing',
            rows_imported = 0,
            last_line = 0,
            started_at = excluded.started_at,
            completed_at = NULL,
            error_message = NULL
        """,
        (relative_path, headers_json, file_size, modified_time_ns, now),
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
            """INSERT INTO records_fts(records_fts, rowid, name_norm)
               SELECT 'delete', id, name_norm FROM records WHERE source_file_id = ?""",
            (source_file_id,),
        )
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
            cursor = connection.executemany(
                """INSERT INTO records(
                    source_file_id, source_line, column_1, column_2, name, birth_date, phone,
                    column_1_norm, column_2_norm, name_norm, birth_date_norm, phone_norm,
                    raw_fields_json, search_text_norm, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                retained,
            )
            # sqlite3.executemany() does not reliably expose all generated IDs. Retrieve by
            # the file/line pairs (unique for this importer) after the insert instead.
            if update_fts:
                for row in retained:
                    source_id, source_line = row[0], row[1]
                    record = connection.execute(
                        "SELECT id, name_norm FROM records "
                        "WHERE source_file_id = ? AND source_line = ?",
                        (source_id, source_line),
                    ).fetchone()
                    connection.execute(
                        "INSERT INTO records_fts(rowid, name_norm) VALUES (?, ?)",
                        (record["id"], record["name_norm"]),
                    )
                    connection.execute(
                        "INSERT INTO records_all_fts(rowid, search_text_norm) "
                        "SELECT id, search_text_norm FROM records WHERE id = ?",
                        (record["id"],),
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
