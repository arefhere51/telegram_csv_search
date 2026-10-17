from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import build_search_indexes, connect, get_metadata, initialize_database


class _LockOnceConnection:
    """Connection proxy that simulates one transient writer-lock collision."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.lock_failures = 0

    def execute(self, sql: str, parameters=()):
        if sql == "BEGIN IMMEDIATE" and self.lock_failures == 0:
            self.lock_failures += 1
            raise sqlite3.OperationalError("database is locked")
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name: str):
        return getattr(self.connection, name)


class _FailOnceIndexConnection:
    """Simulate process failure at the first B-tree index after FTS was committed."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.failed = False

    def execute(self, sql: str, parameters=()):
        if sql.startswith("CREATE INDEX IF NOT EXISTS idx_records_phone_norm") and not self.failed:
            self.failed = True
            raise sqlite3.OperationalError("simulated index build failure")
        return self.connection.execute(sql, parameters)

    def __getattr__(self, name: str):
        return getattr(self.connection, name)


class DatabaseLockingTests(unittest.TestCase):
    def test_schema_7_is_accepted_and_search_activity_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            connection = connect(Path(temporary_directory) / "schema7.db")
            try:
                initialize_database(connection)
                connection.execute(
                    """INSERT INTO search_activity (
                           started_at, telegram_user_id, status, query_text
                       ) VALUES ('2026-10-17T00:00:00+00:00', 123, 'completed', 'test')"""
                )
                connection.execute(
                    "UPDATE metadata SET value = '7' WHERE key = 'schema_version'"
                )
                connection.commit()

                initialize_database(connection)
                self.assertEqual(get_metadata(connection, "schema_version"), "7")
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM search_activity").fetchone()[0], 1
                )
            finally:
                connection.close()

    def test_schema_6_is_upgraded_to_7_without_rebuilding_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            connection = connect(Path(temporary_directory) / "schema6.db")
            try:
                initialize_database(connection)
                connection.execute(
                    "UPDATE metadata SET value = '6' WHERE key = 'schema_version'"
                )
                connection.commit()
                initialize_database(connection)
                self.assertEqual(get_metadata(connection, "schema_version"), "7")
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_activity'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_write_connection_uses_disk_backed_bounded_sqlite_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            connection = connect(Path(temporary_directory) / "memory.db")
            try:
                self.assertEqual(connection.execute("PRAGMA temp_store").fetchone()[0], 1)
                self.assertEqual(connection.execute("PRAGMA cache_size").fetchone()[0], -16 * 1024)
                self.assertEqual(connection.execute("PRAGMA temp.cache_size").fetchone()[0], -4096)
                self.assertEqual(connection.execute("PRAGMA mmap_size").fetchone()[0], 0)
            finally:
                connection.close()

    def test_index_builder_resumes_after_a_btree_stage_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            connection = connect(Path(temporary_directory) / "resume.db")
            try:
                initialize_database(connection)
                proxy = _FailOnceIndexConnection(connection)
                with self.assertRaisesRegex(sqlite3.OperationalError, "simulated index build failure"):
                    build_search_indexes(proxy)

                self.assertEqual(get_metadata(connection, "search_fts_ready"), "1")
                self.assertEqual(get_metadata(connection, "search_indexes_ready"), "0")
                events: list[tuple[str, float]] = []
                build_search_indexes(
                    connection,
                    progress_callback=lambda stage, percent: events.append((stage, percent)),
                )
                self.assertEqual(get_metadata(connection, "search_indexes_ready"), "1")
                self.assertTrue(any(stage == "شاخص متنی FTS از قبل آماده است" for stage, _ in events))
            finally:
                connection.close()

    def test_index_builder_retries_a_transient_sqlite_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "search.db"
            connection = connect(database_path)
            try:
                initialize_database(connection)
                proxy = _LockOnceConnection(connection)
                with patch("database.time.sleep") as sleep:
                    build_search_indexes(proxy)

                self.assertEqual(proxy.lock_failures, 1)
                sleep.assert_called_once_with(1)
                self.assertEqual(get_metadata(connection, "search_indexes_ready"), "1")
            finally:
                connection.close()

    def test_index_builder_reports_progress_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            connection = connect(Path(temporary_directory) / "progress.db")
            events: list[tuple[str, float]] = []
            try:
                initialize_database(connection)
                build_search_indexes(connection, progress_callback=lambda stage, percent: events.append((stage, percent)))
                self.assertEqual(events[0][1], 0.0)
                self.assertEqual(events[-1][1], 100.0)
                self.assertTrue(any("FTS" in stage for stage, _ in events))
                self.assertTrue(any("ساخته شد" in stage for stage, _ in events))
                self.assertEqual([percent for _, percent in events], sorted(percent for _, percent in events))
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
