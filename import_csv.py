#!/usr/bin/env python3
"""Incrementally import CSV files into a disk-backed SQLite search database.

The importer streams one CSV record at a time and writes bounded batches. It never
loads a source file as a whole and can safely re-run after an interrupted import.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import itertools
import logging
from pathlib import Path
import sqlite3
import sys
import time
from typing import Callable, Iterable

from config import ConfigurationError, Settings, load_settings
from database import (
    build_search_indexes,
    connect,
    file_needs_import,
    get_imported_file,
    initialize_database,
    mark_file_completed,
    mark_file_failed,
    mark_file_importing,
    remove_records_for_source,
    search_indexes_ready,
    seed_dedupe_hashes,
    update_import_progress,
    insert_record_batch,
)
from logging_utils import configure_logging
from normalization import normalize_phone, normalize_text

LOGGER = logging.getLogger("telegram_csv_search")
CSV_SUFFIXES = {".csv", ".txt"}


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream CSV files into the Telegram search database."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-import every discovered CSV file, even if its size and modification time match.",
    )
    parser.add_argument(
        "--rebuild-indexes",
        action="store_true",
        help="Rebuild FTS and B-tree search indexes after import. Useful after recovery or maintenance.",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop at the first file error instead of continuing with later files.",
    )
    return parser.parse_args()


def discover_files(data_directory: Path) -> list[Path]:
    """Return source files in deterministic relative-path order."""
    if not data_directory.exists():
        raise FileNotFoundError(f"Data directory does not exist: {data_directory}")
    return sorted(
        (path for path in data_directory.rglob("*") if path.is_file() and path.suffix.lower() in CSV_SUFFIXES),
        key=lambda path: path.relative_to(data_directory).as_posix().casefold(),
    )


def _field_map(raw_fields: list[str], column_names: tuple[str, ...]) -> dict[str, str]:
    values = {name: (raw_fields[index] if index < len(raw_fields) else "") for index, name in enumerate(column_names)}
    # The canonical five fields are always available to the application. Alternative
    # names remain represented losslessly in raw_fields_json.
    return {
        "column_1": values.get("column_1", raw_fields[0] if len(raw_fields) > 0 else ""),
        "column_2": values.get("column_2", raw_fields[1] if len(raw_fields) > 1 else ""),
        "name": values.get("name", raw_fields[2] if len(raw_fields) > 2 else ""),
        "birth_date": values.get("birth_date", raw_fields[3] if len(raw_fields) > 3 else ""),
        "phone": values.get("phone", raw_fields[4] if len(raw_fields) > 4 else ""),
    }


def _header_key(value: str) -> str:
    return normalize_text(value).replace(" ", "_").replace("-", "_")


def _header_names(first_row: list[str], settings: Settings) -> tuple[list[str], bool]:
    """Return display headers and whether the first row is a header.

    Explicit CSV_HAS_HEADER remains authoritative. With it disabled, common
    header labels are still detected per file so uploaded mixed-format CSVs work.
    """
    known = {
        "id", "identifier", "name", "fullname", "full_name", "phone", "mobile",
        "address", "email", "username", "birth_date", "birthdate", "date",
        "national_id", "code", "شناسه", "نام", "نام_خانوادگی", "تلفن", "شماره",
        "آدرس", "ایمیل", "تاریخ_تولد",
    }
    normalized = {_header_key(value) for value in first_row if value.strip()}
    looks_like_header = len(normalized & known) >= 1 and not any(value.strip().isdigit() for value in first_row)
    has_header = settings.csv_has_header or looks_like_header
    if has_header:
        headers = [value.strip() or f"column_{index + 1}" for index, value in enumerate(first_row)]
    else:
        headers = list(settings.csv_column_names)
    return headers, has_header


def _canonical_column_names(headers: list[str], settings: Settings) -> tuple[str, ...]:
    aliases = {
        "column_1": {"column_1", "id", "identifier", "شناسه", "code", "national_id"},
        "column_2": {"column_2", "id_2", "identifier_2"},
        "name": {"name", "fullname", "full_name", "نام", "نام_خانوادگی"},
        "birth_date": {"birth_date", "birthdate", "date_of_birth", "تاریخ_تولد"},
        "phone": {"phone", "mobile", "telephone", "tel", "تلفن", "شماره"},
    }
    result = list(settings.csv_column_names)
    for canonical, names in aliases.items():
        for index, header in enumerate(headers):
            if _header_key(header) in names:
                if index >= len(result):
                    result.extend(f"column_{n}" for n in range(len(result) + 1, index + 2))
                result[index] = canonical
                break
    return tuple(result)


def make_database_row(
    file_id: int,
    source_line: int,
    raw_fields: list[str],
    settings: Settings,
    column_names: tuple[str, ...] | None = None,
) -> tuple:
    """Convert one CSV row into database values while preserving original text."""
    # csv.reader returns strings exactly as parsed, including literal "None". Strip no
    # values: empty fields and whitespace are retained in raw_fields_json and display data.
    fields = _field_map(raw_fields, column_names or settings.csv_column_names)
    column_1_norm = normalize_text(fields["column_1"])
    column_2_norm = normalize_text(fields["column_2"])
    name_norm = normalize_text(fields["name"])
    birth_date_norm = normalize_text(fields["birth_date"])
    phone_norm = normalize_phone(fields["phone"], iran_mode=settings.phone_normalization)
    raw_json = json.dumps(raw_fields, ensure_ascii=False, separators=(",", ":"))
    search_text_norm = normalize_text(" ".join(raw_fields))
    record_hash = hashlib.blake2b(
        raw_json.encode("utf-8"), digest_size=20
    ).hexdigest()
    return (
        file_id,
        source_line,
        fields["column_1"],
        fields["column_2"],
        fields["name"],
        fields["birth_date"],
        fields["phone"],
        column_1_norm,
        column_2_norm,
        name_norm,
        birth_date_norm,
        phone_norm,
        raw_json,
        search_text_norm,
        record_hash,
    )


def format_bytes(byte_count: int) -> str:
    value = float(byte_count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{byte_count} B"


def _progress_message(
    relative_path: str, processed: int, inserted: int, file_size: int, file_handle, started: float
) -> str:
    elapsed = max(time.monotonic() - started, 0.001)
    # TextIOWrapper.tell() can raise after csv.reader has advanced via next().
    # The buffered binary stream still exposes a safe approximate byte position.
    try:
        position = file_handle.tell()
    except (OSError, ValueError):
        try:
            position = file_handle.buffer.tell()
        except (AttributeError, OSError, ValueError):
            position = 0
    percent = min(position / file_size * 100, 100.0) if file_size else 100.0
    return (
        f"Processing {relative_path}: rows processed={processed:,}; rows inserted={inserted:,}; "
        f"elapsed={elapsed:.1f}s; speed={processed / elapsed:,.0f} rows/s; progress={percent:.1f}%"
    )


def import_one_file(
    connection,
    file_path: Path,
    settings: Settings,
    *,
    telegram_metadata: dict[str, object] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Replace one source file atomically at file granularity, committing each batch."""
    relative_path = file_path.relative_to(settings.data_directory).as_posix()
    stat = file_path.stat()
    # Header metadata is determined after opening the source, then persisted on the file.
    file_id = mark_file_importing(connection, relative_path=relative_path,
        file_size=stat.st_size, modified_time_ns=stat.st_mtime_ns,
        telegram_metadata=telegram_metadata)
    # Removing old or partial rows first guarantees safe restart. Completed files are
    # never touched until the importer has decided that a changed/forced reimport is needed.
    removed = remove_records_for_source(connection, file_id)
    if removed:
        LOGGER.info("Removed %s prior rows for %s before re-import.", f"{removed:,}", relative_path)

    rows_processed = 0
    rows_inserted = 0
    batch: list[tuple] = []
    start = time.monotonic()
    last_progress_at = start
    indexes_ready = search_indexes_ready(connection)
    try:
        with file_path.open("r", encoding=settings.csv_encoding, newline="") as source:
            reader = csv.reader(source, delimiter=settings.csv_delimiter)
            first_row = next(reader, None)
            if first_row is None:
                headers, has_header = list(settings.csv_column_names), settings.csv_has_header
                remaining = iter(())
            else:
                headers, has_header = _header_names(first_row, settings)
                remaining = reader if has_header else itertools.chain([first_row], reader)
            canonical_names = _canonical_column_names(headers, settings)
            connection.execute("UPDATE imported_files SET headers_json = ? WHERE id = ?",
                               (json.dumps(headers, ensure_ascii=False), file_id))
            connection.commit()
            start_line = 2 if has_header else 1
            for source_line, raw_fields in enumerate(remaining, start=start_line):
                rows_processed += 1
                batch.append(make_database_row(file_id, source_line, raw_fields, settings, canonical_names))
                if len(batch) >= settings.import_batch_size:
                    rows_inserted += insert_record_batch(
                        connection,
                        batch,
                        use_deduplication=settings.duplicate_mode == "exact",
                        update_fts=indexes_ready,
                    )
                    update_import_progress(connection, file_id, rows_inserted, source_line)
                    connection.commit()
                    batch.clear()
                now = time.monotonic()
                if (
                    rows_processed % settings.import_progress_every_rows == 0
                    or now - last_progress_at >= 2.0
                ):
                    message = _progress_message(relative_path, rows_processed, rows_inserted, stat.st_size, source, start)
                    LOGGER.info(message)
                    if progress_callback:
                        progress_callback(message)
                    last_progress_at = now
        if batch:
            rows_inserted += insert_record_batch(
                connection,
                batch,
                use_deduplication=settings.duplicate_mode == "exact",
                update_fts=indexes_ready,
            )
            update_import_progress(connection, file_id, rows_inserted, source_line if rows_processed else 0)
            connection.commit()
        mark_file_completed(connection, file_id, rows_inserted)
        message = _progress_message(
                relative_path, rows_processed, rows_inserted, stat.st_size,
                # A tiny compatibility object avoids reopening a multi-GB file solely for final progress.
                type("Progress", (), {"tell": lambda _: stat.st_size})(),
                start,
            )
        LOGGER.info(message)
        if progress_callback:
            progress_callback(message)
        return rows_processed, rows_inserted
    except (csv.Error, OSError, UnicodeError, ValueError, sqlite3.Error) as exc:  # type: ignore[name-defined]
        mark_file_failed(connection, file_id, str(exc))
        raise


def main() -> int:
    arguments = parse_arguments()
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    global LOGGER
    LOGGER = configure_logging(settings.log_directory, "importer.log", settings.log_level)
    LOGGER.info("Importer starting. data_directory=%s database=%s", settings.data_directory, settings.database_path)

    try:
        candidates = discover_files(settings.data_directory)
    except OSError as exc:
        LOGGER.error("Cannot discover CSV files: %s", exc)
        return 2
    if not candidates:
        LOGGER.warning("No .csv or .txt source files found in %s", settings.data_directory)
        return 0

    imported_files = skipped_files = failed_files = imported_rows = 0
    connection = connect(settings.database_path)
    try:
        initialize_database(connection)
        if settings.duplicate_mode == "exact":
            seed_dedupe_hashes(connection)
        for file_path in candidates:
            stat = file_path.stat()
            relative_path = file_path.relative_to(settings.data_directory).as_posix()
            existing = get_imported_file(connection, relative_path)
            if not file_needs_import(
                existing,
                file_size=stat.st_size,
                modified_time_ns=stat.st_mtime_ns,
                force=arguments.force,
            ):
                skipped_files += 1
                LOGGER.info("Skipping unchanged completed file: %s", relative_path)
                continue
            try:
                _, inserted = import_one_file(connection, file_path, settings)
                imported_files += 1
                imported_rows += inserted
            except Exception:
                failed_files += 1
                LOGGER.exception("Import failed for %s", relative_path)
                if arguments.stop_on_error:
                    break

        # On a first import, all large writes happen before secondary indexes exist;
        # on later incremental imports indexes stay current batch-by-batch.
        if imported_files or arguments.rebuild_indexes:
            build_search_indexes(connection, force=arguments.rebuild_indexes)
        LOGGER.info(
            "Importer complete: imported_files=%d skipped_files=%d failed_files=%d inserted_records=%d database=%s",
            imported_files, skipped_files, failed_files, imported_rows, format_bytes(settings.database_path.stat().st_size),
        )
    finally:
        connection.close()
    return 1 if failed_files else 0


if __name__ == "__main__":
    raise SystemExit(main())
