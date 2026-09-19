from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
import unittest

from config import Settings
from database import build_search_indexes, connect, initialize_database
from import_csv import import_one_file
from search import search_records
from upload_manager import safe_upload_path


class ImportAndSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.data_directory = root / "data"
        self.data_directory.mkdir()
        self.database_path = root / "database" / "search.db"
        shutil.copy(Path(__file__).with_name("sample_records.csv"), self.data_directory / "sample.csv")
        self.settings = Settings(
            telegram_bot_token="unused-in-tests",
            allowed_user_ids=frozenset({1}),
            data_directory=self.data_directory,
            database_path=self.database_path,
            log_directory=root / "logs",
            csv_delimiter=",",
            csv_encoding="utf-8-sig",
            csv_has_header=False,
            csv_column_names=("column_1", "column_2", "name", "birth_date", "phone"),
            duplicate_mode="keep",
            import_batch_size=2,
            import_progress_every_rows=1000,
            max_results=100,
            results_per_page=10,
            max_query_length=160,
            max_field_display_chars=280,
            phone_normalization=True,
            prefix_search_min_length=3,
            log_level="INFO",
            upload_enabled=True,
            upload_max_bytes=3_000_000_000,
            upload_progress_interval_seconds=3,
            delete_csv_after_import=False,
            telegram_base_url="https://api.telegram.org/bot",
            telegram_file_base_url="https://api.telegram.org/file/bot",
        )
        self.connection = connect(self.database_path)
        initialize_database(self.connection)
        import_one_file(self.connection, self.data_directory / "sample.csv", self.settings)
        build_search_indexes(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def test_import_preserves_all_records_and_exact_phone_search(self) -> None:
        total = self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        self.assertEqual(total, 5)
        result = search_records(self.connection, "9359090161", 0, self.settings)
        self.assertEqual(result.mode, "exact")
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.rows[0]["name"], "روح الله مرادلو")

    def test_persian_normalization_and_fts_prefix_search(self) -> None:
        # Arabic kaf/yeh variants and zero-width joins normalize to the stored Persian name.
        result = search_records(self.connection, "روح‌الله", 0, self.settings)
        self.assertEqual(result.mode, "text")
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.rows[0]["column_1"], "66506301")

    def test_identifier_prefix_search_uses_indexable_range_predicates(self) -> None:
        result = search_records(self.connection, "9359", 0, self.settings)
        self.assertEqual(result.mode, "prefix")
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.rows[0]["phone"], "9359090161")

    def test_reimport_replaces_source_without_duplicate_rows_and_updates_fts(self) -> None:
        import_one_file(self.connection, self.data_directory / "sample.csv", self.settings)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 5)
        result = search_records(self.connection, "غلامحسین", 0, self.settings)
        self.assertEqual(result.total_count, 1)

    def test_bot_module_imports_with_supported_telegram_library(self) -> None:
        # Import validates the python-telegram-bot API surface used by the application.
        import bot

        self.assertTrue(callable(bot.main))

    def test_upload_metadata_columns_and_safe_filename(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(imported_files)")}
        self.assertIn("telegram_filename", columns)
        self.assertIn("telegram_file_id", columns)
        path = safe_upload_path(self.data_directory, "../../private data.csv")
        self.assertEqual(path.parent, self.data_directory)
        self.assertTrue(path.name.endswith("private_data.csv.part"))


if __name__ == "__main__":
    unittest.main()
