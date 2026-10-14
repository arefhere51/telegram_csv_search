from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from config import Settings
from database import connect, initialize_database
from import_csv import _progress_message


class EmptyDatabaseStartupTests(unittest.TestCase):
    def test_empty_schema_can_be_created_without_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = Settings(
                telegram_bot_token="unused", allowed_user_ids=frozenset({1}),
                data_directory=root / "data", database_path=root / "database" / "search.db",
                log_directory=root / "logs", csv_delimiter=",", csv_encoding="utf-8-sig",
                csv_has_header=True, csv_column_names=("column_1", "column_2", "name", "birth_date", "phone"),
                duplicate_mode="keep", import_batch_size=100, import_progress_every_rows=1000,
                max_results=100, results_per_page=10, max_query_length=160, max_field_display_chars=280,
                phone_normalization=True, prefix_search_min_length=3, log_level="INFO",
                upload_enabled=True, upload_max_bytes=3_000_000_000, upload_progress_interval_seconds=3, delete_csv_after_import=False,
                telegram_base_url="https://api.telegram.org/bot", telegram_file_base_url="https://api.telegram.org/file/bot",
                admin_user_ids=frozenset({1}),
            )
            settings.data_directory.mkdir()
            settings.validate_for_bot()
            connection = connect(settings.database_path)
            try:
                initialize_database(connection)
                self.assertTrue(settings.database_path.exists())
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
                self.assertIsNotNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'records_all_fts'"
                ).fetchone())
            finally:
                connection.close()

    def test_progress_message_survives_unavailable_text_tell(self) -> None:
        class BinaryFallback:
            def tell(self):
                return 123

        class Handle:
            buffer = BinaryFallback()

            def tell(self):
                raise OSError("telling position disabled by next() call")

        message = _progress_message("upload.csv", 10, 10, 1000, Handle(), 0.0)
        self.assertIn("پیشرفت: 12.3%", message)


if __name__ == "__main__":
    unittest.main()
