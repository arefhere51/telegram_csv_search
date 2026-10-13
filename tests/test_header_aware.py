from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from config import Settings
from database import build_search_indexes, connect, initialize_database
from import_csv import import_one_file
from search import search_records
from bot import format_record


class HeaderAwareTests(unittest.TestCase):
    def test_headers_drive_search_and_display(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            source = data / "people.csv"
            source.write_text(
                "id,name,phone,address,email\n"
                "7,ثمین,09120000000,تهران میدان انقلاب,samin@example.test\n"
                "8,حسین,09121111111,,hosein@example.test\n",
                encoding="utf-8",
            )
            settings = Settings(
                telegram_bot_token="unused", allowed_user_ids=frozenset({1}),
                data_directory=data, database_path=root / "db.sqlite", log_directory=root / "logs",
                csv_delimiter=",", csv_encoding="utf-8", csv_has_header=True,
                csv_column_names=("column_1", "column_2", "name", "birth_date", "phone"),
                duplicate_mode="keep", import_batch_size=100, import_progress_every_rows=1000,
                max_results=100, results_per_page=10, max_query_length=160, max_field_display_chars=280,
                phone_normalization=True, prefix_search_min_length=3, log_level="INFO",
                upload_enabled=True, upload_max_bytes=3000000000, upload_progress_interval_seconds=3, delete_csv_after_import=False,
                telegram_base_url="https://api.telegram.org/bot", telegram_file_base_url="https://api.telegram.org/file/bot",
            )
            connection = connect(settings.database_path)
            try:
                initialize_database(connection)
                import_one_file(connection, source, settings)
                build_search_indexes(connection)
                result = search_records(connection, "میدان انقلاب", 0, settings)
                self.assertEqual(result.total_count, 1)
                self.assertEqual(json.loads(result.rows[0]["headers_json"])[3], "address")
                rendered = format_record(result.rows[0], 1, settings)
                self.assertIn("<b>address:</b> تهران میدان انقلاب", rendered)
                self.assertNotIn("<b>birth_date:</b>", rendered)
                hosein = search_records(connection, "حسین", 0, settings)
                rendered_hosein = format_record(hosein.rows[0], 1, settings)
                self.assertIn("<b>address:</b> —", rendered_hosein)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
