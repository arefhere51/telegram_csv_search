#!/usr/bin/env python3
"""Incrementally import CSV files into a disk-backed SQLite search database.

The importer streams one CSV record at a time and writes bounded batches. It never
loads a source file as a whole and can safely re-run after an interrupted import.
"""
from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import sys
import time
from typing import BinaryIO, Callable, Iterator

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
    update_file_csv_metadata,
    insert_record_batch,
)
from logging_utils import configure_logging
from normalization import normalize_phone, normalize_text
from progress_utils import RemainingTimeEstimator

LOGGER = logging.getLogger("telegram_csv_search")
CSV_SUFFIXES = {".csv", ".txt"}


class _DecodedBinaryLines:
    """Yield decoded physical lines while exposing exact positions in the byte file."""

    def __init__(self, source: BinaryIO, encoding: str):
        self.source = source
        # CSVs from exports may contain a few malformed bytes even when most of the
        # file is UTF-8. Replace only invalid sequences so a multi-GB import can finish.
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.position = source.tell()
        self.physical_lines = 0
        self._finished = False
        self.replacement_markers = 0

    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        if self._finished:
            raise StopIteration
        raw_line = self.source.readline()
        if raw_line:
            self.position = self.source.tell()
            self.physical_lines += 1
            decoded = self.decoder.decode(raw_line, final=False)
            self.replacement_markers += decoded.count("\ufffd")
            return decoded
        self._finished = True
        tail = self.decoder.decode(b"", final=True)
        self.replacement_markers += tail.count("\ufffd")
        if tail:
            return tail
        raise StopIteration


class OffsetCsvReader:
    """Read CSV records and report their exact half-open byte ranges."""

    def __init__(self, source: BinaryIO, *, encoding: str, delimiter: str):
        self.lines = _DecodedBinaryLines(source, encoding)
        self.reader = csv.reader(self.lines, delimiter=delimiter)

    def read_record(self) -> tuple[list[str], int, int, int] | None:
        start_offset = self.lines.position
        start_line = self.lines.physical_lines + 1
        try:
            fields = next(self.reader)
        except StopIteration:
            return None
        return fields, start_offset, self.lines.position, start_line


def read_csv_header(file_path: Path, settings: Settings) -> list[str]:
    """Read the first CSV record as the upload's header row without loading the file."""
    with file_path.open("rb") as source:
        reader = OffsetCsvReader(
            source, encoding=settings.csv_encoding, delimiter=settings.csv_delimiter
        )
        record = reader.read_record()
    if record is None or not record[0] or not any(value.strip() for value in record[0]):
        raise ValueError("The uploaded CSV is empty or its first row has no column names.")
    return [value.strip() or f"column_{index + 1}" for index, value in enumerate(record[0])]


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
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Compact the SQLite database after import. Can be slow for very large databases.",
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
    # The canonical five fields are always available to the application. Every original
    # value remains in the source CSV and is reconstructed from its byte range on search.
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
    result: list[str] = []
    for index, header in enumerate(headers):
        key = _header_key(header)
        canonical_name = next(
            (canonical for canonical, names in aliases.items() if key in names),
            None,
        )
        if canonical_name is None and index < len(settings.csv_column_names):
            configured_name = settings.csv_column_names[index]
            if key == _header_key(configured_name):
                canonical_name = configured_name
        result.append(canonical_name or f"column_{index + 1}")
    return tuple(result)


def make_database_row(
    file_id: int,
    source_line: int,
    byte_offset_start: int,
    byte_offset_end: int,
    raw_fields: list[str],
    settings: Settings,
    column_names: tuple[str, ...] | None = None,
    selected_column_indices: tuple[int, ...] | list[int] | None = None,
) -> tuple:
    """Build search keys from selected columns while retaining source byte offsets."""
    names = column_names or settings.csv_column_names
    selected = set(selected_column_indices if selected_column_indices is not None else range(len(raw_fields)))
    # Canonical exact/prefix keys are populated only when their source column was selected.
    def selected_value(canonical: str, fallback_index: int) -> str:
        positions = [i for i, name in enumerate(names) if name == canonical and i in selected]
        if not positions and fallback_index in selected and canonical not in names:
            positions = [fallback_index]
        return " ".join(raw_fields[i] for i in positions if i < len(raw_fields))

    column_1_norm = normalize_text(selected_value("column_1", 0))
    column_2_norm = normalize_text(selected_value("column_2", 1))
    name_norm = normalize_text(selected_value("name", 2))
    birth_date_norm = normalize_text(selected_value("birth_date", 3))
    phone_norm = normalize_phone(selected_value("phone", 4), iran_mode=settings.phone_normalization)
    raw_json = json.dumps(raw_fields, ensure_ascii=False, separators=(",", ":"))
    selected_values = [raw_fields[i] for i in sorted(selected) if i < len(raw_fields)]
    search_text_norm = normalize_text(" ".join(selected_values))
    # Deduplicate only within the same source file and the same selection. A global
    # raw-row hash could otherwise let an identical row in another upload suppress
    # its separately selected/searchable columns.
    dedupe_payload = json.dumps(
        [file_id, sorted(selected), raw_fields], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    record_hash = hashlib.blake2b(dedupe_payload, digest_size=20).hexdigest()
    return (
        file_id,
        source_line,
        byte_offset_start,
        byte_offset_end,
        column_1_norm,
        column_2_norm,
        name_norm,
        birth_date_norm,
        phone_norm,
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


def _progress_position(file_handle) -> int:
    """Read a safe approximate byte position after csv.reader advances the stream."""
    try:
        return max(0, int(file_handle.tell()))
    except (OSError, ValueError):
        try:
            return max(0, int(file_handle.buffer.tell()))
        except (AttributeError, OSError, ValueError):
            return 0


def _progress_metrics(file_size: int, file_handle, started: float) -> tuple[float, float]:
    elapsed = max(time.monotonic() - started, 0.001)
    # TextIOWrapper.tell() can raise after csv.reader has advanced via next().
    # The buffered binary stream still exposes a safe approximate byte position.
    position = _progress_position(file_handle)
    percent = min(position / file_size * 100, 100.0) if file_size else 100.0
    return elapsed, percent


def _progress_message(
    relative_path: str, processed: int, inserted: int, file_size: int, file_handle, started: float,
    *, eta_estimator: RemainingTimeEstimator | None = None, finished: bool = False,
) -> str:
    """Localized progress text shown to users in the Telegram chat."""
    elapsed, percent = _progress_metrics(file_size, file_handle, started)
    position = min(_progress_position(file_handle), file_size) if file_size else 0
    eta = (
        eta_estimator.estimate(position, file_size, finished=finished)
        if eta_estimator is not None else "در حال برآورد بر اساس سرعت همین مرحله"
    )
    return (
        f"فایل: {relative_path}؛ ردیف‌های خوانده‌شده: {processed:,}؛ ردیف‌های درج‌شده: {inserted:,}؛ "
        f"زمان: {elapsed:.1f} ثانیه؛ سرعت: {processed / elapsed:,.0f} ردیف/ثانیه؛ پیشرفت: {percent:.1f}%\n"
        f"زمان تقریبی باقی‌مانده برای مرحلهٔ ورود CSV: {eta}؛ ساخت شاخص‌ها جداگانه انجام می‌شود."
    )


def _progress_log_message(
    relative_path: str, processed: int, inserted: int, file_size: int, file_handle, started: float,
) -> str:
    """English progress line written to the server log/console."""
    elapsed, percent = _progress_metrics(file_size, file_handle, started)
    return (
        f"stage=csv_import_progress file={relative_path}; "
        f"rows_read: {processed:,}; rows_inserted: {inserted:,}; "
        f"elapsed: {elapsed:.1f}s; speed: {processed / elapsed:,.0f} rows/s; progress: {percent:.1f}%"
    )


def import_one_file(
    connection,
    file_path: Path,
    settings: Settings,
    *,
    telegram_metadata: dict[str, object] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    selected_column_indices: tuple[int, ...] | list[int] | None = None,
) -> tuple[int, int]:
    """Replace one source file atomically at file granularity, committing each batch."""
    relative_path = file_path.relative_to(settings.data_directory).as_posix()
    stat = file_path.stat()
    LOGGER.info(
        "operation=upload stage=csv_import_open file=%s bytes=%d",
        relative_path, stat.st_size,
    )
    # Header metadata is determined after opening the source, then persisted on the file.
    file_id = mark_file_importing(connection, relative_path=relative_path,
        file_size=stat.st_size, modified_time_ns=stat.st_mtime_ns,
        telegram_metadata=telegram_metadata,
        csv_encoding=settings.csv_encoding,
        csv_delimiter=settings.csv_delimiter)
    # Removing old or partial rows first guarantees safe restart. Completed files are
    # never touched until the importer has decided that a changed/forced reimport is needed.
    removed = remove_records_for_source(connection, file_id)
    if removed:
        LOGGER.info(
            "operation=upload stage=prior_rows_removed rows=%d file=%s",
            removed, relative_path,
        )

    rows_processed = 0
    rows_inserted = 0
    batch: list[tuple] = []
    start = time.monotonic()
    eta_estimator = RemainingTimeEstimator()
    last_progress_at = start
    indexes_ready = search_indexes_ready(connection)
    try:
        with file_path.open("rb") as source:
            reader = OffsetCsvReader(
                source,
                encoding=settings.csv_encoding,
                delimiter=settings.csv_delimiter,
            )
            first_record = reader.read_record()
            if first_record is None:
                headers, has_header = list(settings.csv_column_names), settings.csv_has_header
                pending_record = None
            else:
                first_row, first_start, first_end, first_line = first_record
                if selected_column_indices is not None:
                    # Telegram uploads pass explicit header selections; their first row
                    # is authoritative even if global settings describe headerless files.
                    headers, has_header = [value.strip() or f"column_{i + 1}" for i, value in enumerate(first_row)], True
                else:
                    headers, has_header = _header_names(first_row, settings)
                pending_record = None if has_header else (
                    first_row,
                    first_start,
                    first_end,
                    first_line,
                )
            canonical_names = _canonical_column_names(headers, settings)
            selected_indices = tuple(
                selected_column_indices if selected_column_indices is not None else range(len(headers))
            )
            if any(index < 0 or index >= len(headers) for index in selected_indices):
                raise ValueError("Selected CSV column index is outside the header range.")
            if not selected_indices and rows_processed == 0 and first_record is not None:
                raise ValueError("Select at least one CSV column to make the file searchable.")
            update_file_csv_metadata(
                connection,
                file_id,
                headers_json=json.dumps(headers, ensure_ascii=False),
                column_names_json=json.dumps(canonical_names, ensure_ascii=False),
                selected_columns_json=json.dumps(list(selected_indices)),
                has_header=has_header,
            )
            last_source_line = 0
            while True:
                record = pending_record or reader.read_record()
                pending_record = None
                if record is None:
                    break
                raw_fields, byte_offset_start, byte_offset_end, source_line = record
                if byte_offset_end <= byte_offset_start:
                    raise ValueError(
                        f"Invalid CSV byte range at line {source_line}: "
                        f"{byte_offset_start}..{byte_offset_end}"
                    )
                rows_processed += 1
                last_source_line = source_line
                batch.append(
                    make_database_row(
                        file_id,
                        source_line,
                        byte_offset_start,
                        byte_offset_end,
                        raw_fields,
                        settings,
                        canonical_names,
                        selected_indices,
                    )
                )
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
                    message = _progress_message(
                        relative_path, rows_processed, rows_inserted, stat.st_size, source, start,
                        eta_estimator=eta_estimator,
                    )
                    LOGGER.info(_progress_log_message(
                        relative_path, rows_processed, rows_inserted, stat.st_size, source, start,
                    ))
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
            update_import_progress(connection, file_id, rows_inserted, last_source_line)
            connection.commit()
        if reader.lines.replacement_markers:
            LOGGER.warning(
                "operation=upload stage=decode_replacements count=%d file=%s encoding=%s",
                reader.lines.replacement_markers, relative_path, settings.csv_encoding,
            )
        mark_file_completed(connection, file_id, rows_inserted)
        message = _progress_message(
                relative_path, rows_processed, rows_inserted, stat.st_size,
                # A tiny compatibility object avoids reopening a multi-GB file solely for final progress.
                type("Progress", (), {"tell": lambda _: stat.st_size})(),
                start, eta_estimator=eta_estimator, finished=True,
            )
        LOGGER.info(_progress_log_message(
            relative_path, rows_processed, rows_inserted, stat.st_size,
            type("Progress", (), {"tell": lambda _: stat.st_size})(), start,
        ))
        LOGGER.info(
            "operation=upload stage=csv_import_complete file=%s rows_read=%d rows_inserted=%d",
            relative_path, rows_processed, rows_inserted,
        )
        if progress_callback:
            progress_callback(message)
        return rows_processed, rows_inserted
    except (csv.Error, OSError, UnicodeError, ValueError, sqlite3.Error) as exc:  # type: ignore[name-defined]
        LOGGER.exception(
            "operation=upload stage=csv_import_failed file=%s error_type=%s",
            relative_path, type(exc).__name__,
        )
        try:
            remove_records_for_source(connection, file_id)
        except sqlite3.Error:
            LOGGER.exception(
                "operation=upload stage=partial_row_cleanup_failed file=%s",
                relative_path,
            )
            connection.rollback()
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
    if not candidates and not arguments.vacuum:
        LOGGER.warning("No .csv or .txt source files found in %s", settings.data_directory)
        return 0
    if not candidates:
        LOGGER.info("No CSV/TXT source files found; continuing with database maintenance.")

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
        if arguments.vacuum:
            # VACUUM rewrites the database and returns free pages to the OS.
            # It is opt-in because it can take a long time on multi-GB databases.
            LOGGER.info("Compacting SQLite database with VACUUM...")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("VACUUM")
        LOGGER.info(
            "Importer complete: imported_files=%d skipped_files=%d failed_files=%d inserted_records=%d database=%s",
            imported_files, skipped_files, failed_files, imported_rows, format_bytes(settings.database_path.stat().st_size),
        )
    finally:
        connection.close()
    return 1 if failed_files else 0


if __name__ == "__main__":
    raise SystemExit(main())
