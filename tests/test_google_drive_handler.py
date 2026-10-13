from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot import UPLOAD_SELECTION_KEY, UPLOAD_TRANSFER_KEY, google_drive_link_upload
from google_drive import DriveFileInfo


FILE_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"


class FakeStatusMessage:
    def __init__(self):
        self.message_id = 555
        self.chat_id = 123
        self.text = ""
        self.reply_markup = None
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.reply_markup = kwargs.get("reply_markup")
        self.edits.append(text)
        return self


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.status = FakeStatusMessage()

    async def reply_text(self, text, **kwargs):
        self.status.text = text
        return self.status


class FakeApplication:
    def __init__(self, settings):
        self.bot_data = {"settings": settings}
        self.tasks = []

    def create_task(self, coroutine, **kwargs):
        self.tasks.append(coroutine)


class GoogleDriveHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_share_link_download_uses_header_picker_and_marks_drive_source(self):
        with TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                upload_enabled=True,
                upload_max_bytes=1000,
                upload_progress_interval_seconds=0.01,
                google_drive_configured=True,
                admin_user_ids=frozenset({123}),
                data_directory=Path(directory),
                csv_encoding="utf-8-sig",
                csv_delimiter=",",
            )
            application = FakeApplication(settings)
            message = FakeMessage(f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing")
            update = SimpleNamespace(
                effective_message=message,
                effective_chat=SimpleNamespace(type="private", id=123),
                effective_user=SimpleNamespace(id=123),
            )
            context = SimpleNamespace(application=application, user_data={})

            def fake_download(settings, reference, destination, *, max_bytes, progress_callback=None):
                payload = "name,phone,address\nلیلا,09120000000,تهران\n".encode("utf-8")
                destination.write_bytes(payload)
                if progress_callback:
                    progress_callback("customers.csv", 0, len(payload))
                    progress_callback("customers.csv", len(payload), len(payload))
                return DriveFileInfo(reference.file_id, "customers.csv", len(payload), "text/csv")

            with patch("bot.download_drive_csv", side_effect=fake_download):
                await google_drive_link_upload(update, context)
                self.assertEqual(context.user_data[UPLOAD_TRANSFER_KEY], "google_drive")
                self.assertEqual(len(application.tasks), 1)
                await application.tasks[0]

            self.assertNotIn(UPLOAD_TRANSFER_KEY, context.user_data)
            selection = context.user_data[UPLOAD_SELECTION_KEY]
            self.assertEqual(selection.headers, ("name", "phone", "address"))
            self.assertEqual(selection.job.source_kind, "google_drive")
            self.assertEqual(selection.job.original_filename, "customers.csv")
            self.assertEqual(selection.job.file_size, len("name,phone,address\nلیلا,09120000000,تهران\n".encode("utf-8")))
            self.assertTrue(selection.job.path.is_file())
            self.assertIsNotNone(message.status.reply_markup)
            self.assertIn("ستون‌هایی را که باید در جست‌وجو باشند انتخاب کنید", message.status.text)


if __name__ == "__main__":
    unittest.main()
