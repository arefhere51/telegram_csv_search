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


class DatabaseLockingTests(unittest.TestCase):
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
