"""Indexed search queries for the imported records database."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Sequence

from config import Settings
from normalization import fts_prefix_query, is_identifier_query, normalize_phone, normalize_text


@dataclass(frozen=True, slots=True)
class SearchPage:
    rows: Sequence[sqlite3.Row]
    total_count: int
    mode: str


def _rows_and_count(
    connection: sqlite3.Connection,
    where_sql: str,
    parameters: Sequence[object],
    page: int,
    settings: Settings,
) -> tuple[Sequence[sqlite3.Row], int]:
    offset = page * settings.results_per_page
    base_sql = (
        "SELECT r.id, r.column_1, r.column_2, r.name, r.birth_date, r.phone, "
        "r.raw_fields_json, f.headers_json, f.relative_path AS source_file, r.source_line "
        "FROM records r JOIN imported_files f ON f.id = r.source_file_id "
    )
    count_sql = "SELECT COUNT(*) FROM records r "
    rows = connection.execute(
        base_sql + where_sql + " ORDER BY r.id LIMIT ? OFFSET ?",
        tuple(parameters) + (settings.results_per_page, offset),
    ).fetchall()
    total_count = int(connection.execute(count_sql + where_sql, tuple(parameters)).fetchone()[0])
    return rows, total_count


def _fts_rows_and_count(
    connection: sqlite3.Connection,
    fts_query: str,
    page: int,
    settings: Settings,
) -> tuple[Sequence[sqlite3.Row], int]:
    offset = page * settings.results_per_page
    base_sql = (
        "SELECT r.id, r.column_1, r.column_2, r.name, r.birth_date, r.phone, "
        "r.raw_fields_json, f.headers_json, f.relative_path AS source_file, r.source_line "
        "FROM records_fts JOIN records r ON r.id = records_fts.rowid "
        "JOIN imported_files f ON f.id = r.source_file_id "
        "WHERE records_fts MATCH ? "
    )
    rows = connection.execute(
        base_sql + "ORDER BY records_fts.rank, r.id LIMIT ? OFFSET ?",
        (fts_query, settings.results_per_page, offset),
    ).fetchall()
    total_count = int(
        connection.execute("SELECT COUNT(*) FROM records_fts WHERE records_fts MATCH ?", (fts_query,)).fetchone()[0]
    )
    return rows, total_count


def _all_fields_rows_and_count(connection: sqlite3.Connection, fts_query: str, page: int, settings: Settings) -> tuple[Sequence[sqlite3.Row], int]:
    offset = page * settings.results_per_page
    base_sql = (
        "SELECT r.id, r.column_1, r.column_2, r.name, r.birth_date, r.phone, "
        "r.raw_fields_json, f.headers_json, f.relative_path AS source_file, r.source_line "
        "FROM records_all_fts JOIN records r ON r.id = records_all_fts.rowid "
        "JOIN imported_files f ON f.id = r.source_file_id WHERE records_all_fts MATCH ? "
    )
    rows = connection.execute(base_sql + "ORDER BY records_all_fts.rank, r.id LIMIT ? OFFSET ?", (fts_query, settings.results_per_page, offset)).fetchall()
    total = int(connection.execute("SELECT COUNT(*) FROM records_all_fts WHERE records_all_fts MATCH ?", (fts_query,)).fetchone()[0])
    return rows, total


def _exact_search(
    connection: sqlite3.Connection, query: str, page: int, settings: Settings
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
    )
    return SearchPage(rows, total, "exact")


def _prefix_upper_bound(prefix: str) -> str:
    # Every string that begins with prefix sorts below this value in SQLite's BINARY order.
    return prefix + "\U0010ffff"


def _identifier_prefix_search(
    connection: sqlite3.Connection, query: str, page: int, settings: Settings
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
    )
    return SearchPage(rows, total, "prefix")


def search_records(
    connection: sqlite3.Connection, query: str, page: int, settings: Settings
) -> SearchPage:
    """Search exact values first, then indexed identifier prefixes or FTS name prefixes."""
    safe_query = query.strip()
    if not safe_query:
        return SearchPage((), 0, "empty")
    if len(safe_query) > settings.max_query_length:
        raise ValueError(f"Queries may contain at most {settings.max_query_length} characters.")
    if page < 0:
        page = 0

    exact = _exact_search(connection, safe_query, page, settings)
    if exact.total_count:
        return exact

    if is_identifier_query(safe_query):
        return _identifier_prefix_search(connection, safe_query, page, settings)

    fts_query = fts_prefix_query(normalize_text(safe_query))
    if not fts_query:
        return SearchPage((), 0, "text")
    rows, total = _all_fields_rows_and_count(connection, fts_query, page, settings)
    return SearchPage(rows, total, "text")


def parse_raw_fields(row: sqlite3.Row) -> list[str]:
    """Defensively decode the preserved raw CSV row for optional display."""
    try:
        value = json.loads(row["raw_fields_json"])
        return [str(item) for item in value] if isinstance(value, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
