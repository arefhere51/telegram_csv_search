from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest

from bot import _archive_and_delete_database_file
from database import (
    build_search_indexes,
    connect,
    count_completed_files,
    initialize_database,
    list_completed_files,
)
from import_csv import _progress_log_message, _progress_message, discover_files, import_one_file


class DatabaseManagementTests(unittest.TestCase):
    def test_delete_removes_search_rows_and_archives_source_outside_import_directory(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data_directory = root / "data"
            data_directory.mkdir()
            source = data_directory / "File-no43.csv"
            source.write_text(
                "name,phone\nAli,09120000000\nLeila,09121111111\n",
                encoding="utf-8",
            )
            database_path = root / "database" / "search.db"
            settings = SimpleNamespace(
                data_directory=data_directory,
                database_path=database_path,
                csv_encoding="utf-8-sig",
                csv_delimiter=",",
                csv_has_header=True,
                csv_column_names=("name", "phone"),
                duplicate_mode="keep",
                import_batch_size=10,
                import_progress_every_rows=1000,
                phone_normalization=False,
            )

            connection = connect(database_path)
            try:
                initialize_database(connection)
                import_one_file(connection, source, settings, selected_column_indices=(0,))
                build_search_indexes(connection)
                file_id = int(connection.execute("SELECT id FROM imported_files").fetchone()[0])
                self.assertEqual(count_completed_files(connection), 1)
                self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]), 2)
            finally:
                connection.close()

            progress_events: list[tuple[str, float]] = []
            result = _archive_and_delete_database_file(
                settings,
                file_id,
                progress_callback=lambda stage, percent: progress_events.append((stage, percent)),
            )
            self.assertEqual(progress_events[0][1], 0.0)
            self.assertEqual(progress_events[-1][1], 100.0)
            self.assertTrue(any("آرشیو" in stage for stage, _ in progress_events))
            self.assertIsNotNone(result)
            deleted, archived_path = result
            self.assertEqual(deleted["record_count"], 2)
            self.assertIsNotNone(archived_path)
            self.assertTrue(archived_path.is_file())
            self.assertFalse(source.exists())
            self.assertEqual(discover_files(data_directory), [])

            connection = connect(database_path, readonly=True)
            try:
                self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM imported_files").fetchone()[0]), 0)
                self.assertEqual(int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]), 0)
                matches = connection.execute(
                    "SELECT COUNT(*) FROM records_all_fts WHERE records_all_fts MATCH ?",
                    ("ali*",),
                ).fetchone()[0]
                self.assertEqual(int(matches), 0)
                self.assertEqual(list_completed_files(connection, limit=8, offset=0), [])
            finally:
                connection.close()

    def test_import_progress_stays_persian_in_chat_but_is_english_in_logs(self) -> None:
        handle = BytesIO(b"x" * 100)
        started = time.monotonic() - 1
        chat_text = _progress_message("File-no43.csv", 10, 10, 100, handle, started)
        log_text = _progress_log_message("File-no43.csv", 10, 10, 100, handle, started)
        self.assertIn("ردیف", chat_text)
        self.assertIn("stage=csv_import_progress", log_text)
        self.assertIn("file=File-no43.csv", log_text)
        self.assertIn("rows_inserted:", log_text)
        self.assertFalse(any("\u0600" <= char <= "\u06ff" for char in log_text))


if __name__ == "__main__":
    unittest.main()
