from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from config import Settings
from database import connect
from search import search_records
from upload_manager import UploadJob, UploadManager


class _FakeBot:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_message_text(self, *, chat_id: int, message_id: int, text: str) -> None:
        self.edits.append(text)

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.edits.append(text)


class UploadRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_setting_is_ignored_and_source_remains_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            data.mkdir()
            source = data / "telegram-upload.csv"
            source.write_text(
                "id,name,phone\n1,نگار,09120000000\n",
                encoding="utf-8-sig",
            )
            settings = Settings(
                telegram_bot_token="unused",
                allowed_user_ids=frozenset({1}),
                data_directory=data,
                database_path=root / "database" / "search.db",
                log_directory=root / "logs",
                csv_delimiter=",",
                csv_encoding="utf-8-sig",
                csv_has_header=True,
                csv_column_names=("column_1", "column_2", "name", "birth_date", "phone"),
                duplicate_mode="keep",
                import_batch_size=100,
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
                delete_csv_after_import=True,
                telegram_base_url="https://api.telegram.org/bot",
                telegram_file_base_url="https://api.telegram.org/file/bot",
            )
            bot = _FakeBot()
            manager = UploadManager(settings, bot)  # type: ignore[arg-type]
            await manager._process(
                UploadJob(
                    path=source,
                    original_filename="people.csv",
                    file_id="file-id",
                    file_unique_id="unique-id",
                    file_size=source.stat().st_size,
                    uploader_id=1,
                    chat_id=1,
                    status_message_id=1,
                ),
                1,
            )

            self.assertTrue(source.exists())
            self.assertTrue(any("منبع داده نگه داشته شد" in text for text in bot.edits))
            connection = connect(settings.database_path, readonly=True)
            try:
                result = search_records(connection, "نگار", 0, settings)
                self.assertEqual(result.total_count, 1)
                self.assertEqual(result.rows[0]["phone"], "09120000000")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
