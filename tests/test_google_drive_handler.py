from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bot import (
    UPLOAD_CANCELLATIONS_KEY,
    UPLOAD_SELECTION_KEY,
    UPLOAD_TRANSFER_KEY,
    _download_drive_upload,
    google_drive_link_upload,
    upload_selection_callback,
)
from google_drive import DriveFileInfo, DriveFileReference


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


class FakeCallback:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_reply_markup(self, *, reply_markup=None):
        self.message.reply_markup = reply_markup

    async def edit_message_text(self, text, **kwargs):
        return await self.message.edit_text(text, **kwargs)


class FakeUploadManager:
    def __init__(self):
        self.jobs = []

    async def enqueue(self, job):
        self.jobs.append(job)
        return 1


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
            headers = tuple(f"header_{index}" for index in range(10))
            values = tuple(f"value_{index}" for index in range(10))
            payload = (",".join(headers) + "\n" + ",".join(values) + "\n").encode("utf-8")

            def fake_download(settings, reference, destination, *, max_bytes, progress_callback=None):
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
            self.assertEqual(selection.headers, tuple(f"header_{index}" for index in range(10)))
            self.assertEqual(selection.job.source_kind, "google_drive")
            self.assertEqual(selection.job.original_filename, "customers.csv")
            self.assertEqual(selection.job.file_size, len(payload))
            self.assertTrue(selection.job.path.is_file())
            self.assertIsNotNone(message.status.reply_markup)
            self.assertIn("ستون‌هایی را که باید در جست‌وجو باشند انتخاب کنید", message.status.text)

            callback_update = lambda callback: SimpleNamespace(
                callback_query=callback,
                effective_chat=SimpleNamespace(type="private", id=123),
                effective_user=SimpleNamespace(id=123),
            )

            toggle = FakeCallback(f"u:{selection.token}:t:0", message.status)
            await upload_selection_callback(callback_update(toggle), context)
            self.assertEqual(selection.selected, {0})
            self.assertEqual(len(toggle.answers), 1)

            next_page = FakeCallback(f"u:{selection.token}:p:1", message.status)
            await upload_selection_callback(callback_update(next_page), context)
            self.assertEqual(selection.page, 1)
            self.assertIn("صفحهٔ 2 از 2", message.status.text)
            self.assertEqual(message.status.reply_markup.inline_keyboard[0][0].text, "← قبلی")

            toggle_second_page = FakeCallback(f"u:{selection.token}:t:8", message.status)
            await upload_selection_callback(callback_update(toggle_second_page), context)
            self.assertEqual(selection.selected, {0, 8})

            manager = FakeUploadManager()
            application.bot_data["upload_manager"] = manager
            confirm = FakeCallback(f"u:{selection.token}:done", message.status)
            await upload_selection_callback(callback_update(confirm), context)
            self.assertEqual(len(manager.jobs), 1)
            self.assertEqual(manager.jobs[0].selected_column_indices, (0, 8))
            self.assertNotIn(UPLOAD_SELECTION_KEY, context.user_data)

    async def test_start_cancellation_stops_drive_stream_and_removes_partial_file(self):
        with TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                upload_progress_interval_seconds=0.01,
                upload_max_bytes=1000,
                data_directory=Path(directory),
                csv_encoding="utf-8-sig",
                csv_delimiter=",",
            )
            cancellation_event = Event()
            stream_started = Event()
            resume_stream = Event()
            status = FakeStatusMessage()
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=123),
                effective_chat=SimpleNamespace(id=123),
            )
            application = FakeApplication(settings)
            application.bot_data[UPLOAD_CANCELLATIONS_KEY] = {123: cancellation_event}
            context = SimpleNamespace(application=application, user_data={UPLOAD_TRANSFER_KEY: "google_drive"})
            temporary_path = Path(directory) / ".drive-partial.csv.part"

            def fake_download(settings, reference, destination, *, max_bytes, progress_callback=None):
                destination.write_bytes(b"partial")
                stream_started.set()
                resume_stream.wait(2)
                progress_callback("customers.csv", 7, 7)
                return DriveFileInfo(reference.file_id, "customers.csv", 7, "text/csv")

            with patch("bot.download_drive_csv", side_effect=fake_download):
                task = asyncio.create_task(_download_drive_upload(
                    update,
                    context,
                    settings,
                    DriveFileReference("1AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
                    temporary_path,
                    status,
                    cancellation_event,
                ))
                self.assertTrue(await asyncio.to_thread(stream_started.wait, 1))
                cancellation_event.set()
                resume_stream.set()
                await asyncio.wait_for(task, timeout=2)

            self.assertFalse(temporary_path.exists())
            self.assertIn("دریافت فایل لغو شد", status.text)
            self.assertNotIn(UPLOAD_TRANSFER_KEY, context.user_data)
            self.assertNotIn(123, application.bot_data[UPLOAD_CANCELLATIONS_KEY])


if __name__ == "__main__":
    unittest.main()
