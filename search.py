"""Indexed search queries for the imported records database."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Mapping, Sequence

from config import Settings
from normalization import fts_prefix_query, is_identifier_query, normalize_phone, normalize_text


class HydratedRow(Mapping[str, Any]):
    """A sqlite3.Row-compatible mapping backed by the authoritative CSV row."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]):
        self._data = data

    def __getitem__(self, key: str | int | slice) -> Any:
        if isinstance(key, (int, slice)):
            return tuple(self._data.values())[key]
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def keys(self):
        return self._data.keys()


@dataclass(frozen=True, slots=True)
class SearchPage:
    # Keep the established public annotation; HydratedRow provides the same key,
    # positional-index, iteration, and keys() access used by sqlite3.Row consumers.
    rows: Sequence[sqlite3.Row]
    total_count: int
    mode: str


_HIT_COLUMNS = (
    "r.id, r.byte_offset_start, r.byte_offset_end, f.headers_json, "
    "f.column_names_json, f.csv_encoding, f.csv_delimiter, "
    "f.file_size, f.modified_time_ns, f.relative_path AS source_file, r.source_line"
)


def _field_map(raw_fields: list[str], column_names: tuple[str, ...]) -> dict[str, str]:
    values = {
        name: (raw_fields[index] if index < len(raw_fields) else "")
        for index, name in enumerate(column_names)
    }
    return {
        "column_1": values.get("column_1", raw_fields[0] if len(raw_fields) > 0 else ""),
        "column_2": values.get("column_2", raw_fields[1] if len(raw_fields) > 1 else ""),
        "name": values.get("name", raw_fields[2] if len(raw_fields) > 2 else ""),
        "birth_date": values.get("birth_date", raw_fields[3] if len(raw_fields) > 3 else ""),
        "phone": values.get("phone", raw_fields[4] if len(raw_fields) > 4 else ""),
    }


def _json_string_list(value: object, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    if not isinstance(decoded, list):
        return fallback
    return tuple(str(item) for item in decoded)


def _source_path(relative_path: str, settings: Settings) -> Path:
    data_root = settings.data_directory.resolve()
    path = (data_root / relative_path).resolve()
    try:
        path.relative_to(data_root)
    except ValueError as exc:
        raise OSError(f"CSV source path escapes DATA_DIRECTORY: {relative_path}") from exc
    return path


def _hydrate_row(hit: sqlite3.Row, settings: Settings) -> HydratedRow:
    source_path = _source_path(str(hit["source_file"]), settings)
    start = int(hit["byte_offset_start"])
    end = int(hit["byte_offset_end"])
    if end <= start:
        raise OSError(f"Invalid CSV byte range {start}..{end} for {source_path}")
    try:
        with source_path.open("rb") as source:
            stat = source_path.stat()
            if (
                stat.st_size != int(hit["file_size"])
                or stat.st_mtime_ns != int(hit["modified_time_ns"])
            ):
                raise OSError(
                    f"CSV source file changed after import and must be re-imported: {source_path}"
                )
            source.seek(start)
            raw_record = source.read(end - start)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"CSV source file is required for search hydration but is missing: {source_path}"
        ) from exc
    if len(raw_record) != end - start:
        raise OSError(f"CSV source file changed or was truncated after import: {source_path}")
    encoding = str(hit["csv_encoding"])
    delimiter = str(hit["csv_delimiter"])
    try:
        decoded = raw_record.decode(encoding)
        parsed = list(csv.reader(io.StringIO(decoded, newline=""), delimiter=delimiter))
    except (UnicodeError, csv.Error) as exc:
        raise OSError(
            f"Could not hydrate CSV record at {source_path}:{hit['source_line']}"
        ) from exc
    if len(parsed) != 1:
        raise OSError(
            f"Stored CSV byte range does not contain exactly one record at "
            f"{source_path}:{hit['source_line']}"
        )
    raw_fields = parsed[0]
    column_names = _json_string_list(hit["column_names_json"], settings.csv_column_names)
    fields = _field_map(raw_fields, column_names)
    return HydratedRow(
        {
            "id": hit["id"],
            **fields,
            # Preserve bot.format_record/parse_raw_fields compatibility, but construct
            # this value only after reading the authoritative CSV source.
            "raw_fields_json": json.dumps(raw_fields, ensure_ascii=False, separators=(",", ":")),
            "headers_json": hit["headers_json"],
            "source_file": hit["source_file"],
            "source_line": hit["source_line"],
        }
    )


def _hydrate_rows(rows: Sequence[sqlite3.Row], settings: Settings) -> Sequence[HydratedRow]:
    return tuple(_hydrate_row(row, settings) for row in rows)


def _rows_and_count(
    connection: sqlite3.Connection,
    where_sql: str,
    parameters: Sequence[object],
    page: int,
    settings: Settings,
    *,
    count_total: bool = True,
) -> tuple[Sequence[sqlite3.Row], int]:
    offset = page * settings.results_per_page
    base_sql = (
        f"SELECT {_HIT_COLUMNS} "
        "FROM records r JOIN imported_files f ON f.id = r.source_file_id "
    )
    count_sql = "SELECT COUNT(*) FROM records r "
    rows = connection.execute(
        base_sql + where_sql + " ORDER BY r.id LIMIT ? OFFSET ?",
        tuple(parameters) + (settings.results_per_page, offset),
    ).fetchall()
    total_count = (
        int(connection.execute(count_sql + where_sql, tuple(parameters)).fetchone()[0])
        if count_total else 0
    )
    return _hydrate_rows(rows, settings), total_count


def _all_fields_rows_and_count(
    connection: sqlite3.Connection,
    fts_query: str,
    page: int,
    settings: Settings,
    *,
    count_total: bool = True,
) -> tuple[Sequence[sqlite3.Row], int]:
    offset = page * settings.results_per_page
    base_sql = (
        f"SELECT {_HIT_COLUMNS} "
        "FROM records_all_fts JOIN records r ON r.id = records_all_fts.rowid "
        "JOIN imported_files f ON f.id = r.source_file_id WHERE records_all_fts MATCH ? "
    )
    rows = connection.execute(base_sql + "ORDER BY records_all_fts.rank, r.id LIMIT ? OFFSET ?", (fts_query, settings.results_per_page, offset)).fetchall()
    total = (
        int(connection.execute("SELECT COUNT(*) FROM records_all_fts WHERE records_all_fts MATCH ?", (fts_query,)).fetchone()[0])
        if count_total else 0
    )
    return _hydrate_rows(rows, settings), total


def _exact_search(
    connection: sqlite3.Connection, query: str, page: int, settings: Settings, *, count_total: bool = True
) -> SearchPage:
    normalized = normalize_text(query)
    phone = normalize_phone(query, iran_mode=settings.phone_normalization)
    # Never compare an empty normalized value: a query containing Persian letters must
    # not accidentally match every record whose phone is blank.
    clauses = [
        "r.column_1_norm = ?",
        "r.column_2_norm = ?",
        "r.name_norm = ?",
        "r.birth_date_norm = ?",
    ]
    parameters: list[str] = [normalized, normalized, normalized, normalized]
    if is_identifier_query(query) and phone:
        clauses.insert(0, "r.phone_norm = ?")
        parameters.insert(0, phone)
    # Each term has a dedicated B-tree index. SQLite can combine these indexed clauses.
    where = "WHERE " + " OR ".join(clauses)
    rows, total = _rows_and_count(
        connection,
        where,
        parameters,
        page,
        settings,
        count_total=count_total,
    )
    return SearchPage(rows, total, "exact")


def _prefix_upper_bound(prefix: str) -> str:
    # Every string that begins with prefix sorts below this value in SQLite's BINARY order.
    return prefix + "\U0010ffff"


def _identifier_prefix_search(
    connection: sqlite3.Connection, query: str, page: int, settings: Settings, *, count_total: bool = True
) -> SearchPage:
    prefix = normalize_phone(query, iran_mode=settings.phone_normalization)
    text_prefix = normalize_text(query)
    if len(prefix) < settings.prefix_search_min_length:
        return SearchPage((), 0, "prefix")
    text_upper = _prefix_upper_bound(text_prefix)
    phone_prefixes = {prefix}
    # A short mobile fragment such as 9359 should also find an Iranian number stored
    # canonically as 09359..., while a user-entered 09359 already works unchanged.
    raw_digits = "".join(character for character in text_prefix if character.isdigit())
    if settings.phone_normalization and raw_digits.startswith("9") and len(raw_digits) < 10:
        phone_prefixes.add("0" + raw_digits)
    phone_clauses = " OR ".join("(r.phone_norm >= ? AND r.phone_norm < ?)" for _ in phone_prefixes)
    phone_parameters: list[str] = []
    for candidate in sorted(phone_prefixes):
        phone_parameters.extend((candidate, _prefix_upper_bound(candidate)))
    # Range predicates use B-tree indexes, unlike a contains '%value%' scan. The UI and
    # documentation deliberately describe identifier partial search as a prefix search.
    where = (
        "WHERE (" + phone_clauses + ") "
        "OR (r.column_1_norm >= ? AND r.column_1_norm < ?) "
        "OR (r.column_2_norm >= ? AND r.column_2_norm < ?) "
        "OR (r.birth_date_norm >= ? AND r.birth_date_norm < ?)"
    )
    rows, total = _rows_and_count(
        connection,
        where,
        tuple(phone_parameters) + (prefix, _prefix_upper_bound(prefix), prefix, _prefix_upper_bound(prefix), text_prefix, text_upper),
        page,
        settings,
        count_total=count_total,
    )
    return SearchPage(rows, total, "prefix")


def search_records(
    connection: sqlite3.Connection,
    query: str,
    page: int,
    settings: Settings,
    *,
    mode: str | None = None,
    count_total: bool = True,
) -> SearchPage:
    """Search exact values first, then indexed prefixes or FTS prefixes in selected columns."""
    safe_query = query.strip()
    if not safe_query:
        return SearchPage((), 0, "empty")
    if len(safe_query) > settings.max_query_length:
        raise ValueError(f"Queries may contain at most {settings.max_query_length} characters.")
    if page < 0:
        page = 0

    if mode is not None:
        if mode == "exact":
            return _exact_search(connection, safe_query, page, settings, count_total=count_total)
        if mode == "prefix":
            return _identifier_prefix_search(connection, safe_query, page, settings, count_total=count_total)
        if mode == "text":
            fts_query = fts_prefix_query(normalize_text(safe_query))
            if not fts_query:
                return SearchPage((), 0, "text")
            rows, total = _all_fields_rows_and_count(
                connection, fts_query, page, settings, count_total=count_total
            )
            return SearchPage(rows, total, "text")
        raise ValueError(f"Unsupported search mode: {mode}")

    exact = _exact_search(connection, safe_query, page, settings, count_total=count_total)
    if exact.total_count or (mode is None and not count_total):
        return exact

    if is_identifier_query(safe_query):
        prefix_result = _identifier_prefix_search(
            connection, safe_query, page, settings, count_total=count_total
        )
        if prefix_result.total_count:
            return prefix_result

    # FTS contains only the columns selected for this source file. Falling through also
    # lets numeric/custom-header columns participate after the canonical prefix indexes.
    fts_query = fts_prefix_query(normalize_text(safe_query))
    if not fts_query:
        return SearchPage((), 0, "text")
    rows, total = _all_fields_rows_and_count(
        connection, fts_query, page, settings, count_total=count_total
    )
    return SearchPage(rows, total, "text")


def parse_raw_fields(row: Mapping[str, Any] | sqlite3.Row) -> list[str]:
    """Defensively decode the preserved raw CSV row for optional display."""
    try:
        value = json.loads(row["raw_fields_json"])
        return [str(item) for item in value] if isinstance(value, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
