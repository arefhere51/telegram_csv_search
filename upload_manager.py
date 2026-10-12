"""Private-chat Telegram document ingestion with live progress updates."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import secrets
import time
from typing import Callable

from telegram import Bot
from telegram.error import BadRequest

from config import Settings
from database import build_search_indexes, connect, initialize_database, seed_dedupe_hashes
from import_csv import import_one_file

LOGGER = logging.getLogger("telegram_csv_search")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class UploadJob:
    path: Path
    original_filename: str
    file_id: str
    file_unique_id: str | None
    file_size: int | None
    uploader_id: int
    chat_id: int
    status_message_id: int | None = None
    selected_column_indices: tuple[int, ...] | None = None


class UploadManager:
    """A single queue prevents simultaneous imports from writing the same SQLite DB."""

    def __init__(self, settings: Settings, bot: Bot):
        self.settings = settings
        self.bot = bot
        self.queue: asyncio.Queue[tuple[UploadJob, int]] = asyncio.Queue()
        self.worker_task: asyncio.Task[None] | None = None
        self.stopping = False

    async def start(self) -> None:
        self.stopping = False
        self.worker_task = asyncio.create_task(self._worker(), name="csv-upload-import-worker")

    async def stop(self) -> None:
        self.stopping = True
        if self.worker_task:
            self.worker_task.cancel()
            await asyncio.gather(self.worker_task, return_exceptions=True)
            self.worker_task = None

    async def enqueue(self, job: UploadJob) -> int:
        position = self.queue.qsize() + 1
        await self.queue.put((job, position))
        return position

    @staticmethod
    def _bar(percent: float, width: int = 20) -> str:
        percent = max(0.0, min(percent, 100.0))
        filled = round(width * percent / 100)
        return "[" + "█" * filled + "░" * (width - filled) + "]"

    def _format_download(self, filename: str, received: int, total: int | None, speed: float) -> str:
        if total:
            percent = received / total * 100
            progress = f"{self._bar(percent)} {percent:5.1f}%"
            size = f"{_format_bytes(received)} / {_format_bytes(total)}"
        else:
            progress = f"{self._bar(0)} --.-%"
            size = _format_bytes(received)
        return (
            f"⬇️ دانلود فایل\n{filename}\n{progress}\n"
            f"حجم: {size}\nسرعت: {_format_bytes(speed)}/s"
        )

    def _format_import(self, message: str) -> str:
        # import_csv emits a stable human-readable line with progress, rows and speed.
        return f"📥 ایمپورت فایل\n{message}"

    async def _edit_status(self, job: UploadJob, text: str) -> None:
        if not job.status_message_id:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=job.chat_id,
                message_id=job.status_message_id,
                text=text,
            )
        except BadRequest as exc:
            # Telegram returns this when the text is identical to the previous edit.
            if "not modified" not in str(exc).lower():
                LOGGER.warning("Could not edit upload status: %s", exc)
        except Exception:
            LOGGER.exception("Could not edit upload status chat_id=%s", job.chat_id)

    async def _status(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            LOGGER.exception("Could not send upload status chat_id=%s", chat_id)

    async def _worker(self) -> None:
        while True:
            job, position = await self.queue.get()
            try:
                await self._process(job, position)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Unhandled upload worker error filename=%s", job.original_filename)
                await self._status(job.chat_id, f"Import failed for {job.original_filename}. The CSV was kept on the server.")
            finally:
                self.queue.task_done()

    async def _process(self, job: UploadJob, position: int) -> None:
        await self._edit_status(job, f"⏳ در صف ایمپورت\n{job.original_filename}\nموقعیت در صف: {position}")
        loop = asyncio.get_running_loop()
        last_status = 0.0

        def progress(message: str) -> None:
            nonlocal last_status
            now = time.monotonic()
            if now - last_status < self.settings.upload_progress_interval_seconds:
                return
            last_status = now
            future = asyncio.run_coroutine_threadsafe(
                self._edit_status(job, self._format_import(message)), loop
            )
            future.add_done_callback(lambda completed: completed.exception() if not completed.cancelled() else None)

        metadata = {
            "file_id": job.file_id,
            "file_unique_id": job.file_unique_id,
            "filename": job.original_filename,
            "file_size": job.file_size,
            "uploader_id": job.uploader_id,
            "chat_id": job.chat_id,
        }

        def import_sync() -> tuple[int, int]:
            connection = connect(self.settings.database_path)
            try:
                initialize_database(connection)
                if self.settings.duplicate_mode == "exact":
                    seed_dedupe_hashes(connection)
                processed, inserted = import_one_file(
                    connection, job.path, self.settings,
                    telegram_metadata=metadata,
                    progress_callback=progress,
                    selected_column_indices=job.selected_column_indices,
                )
                build_search_indexes(connection)
                return processed, inserted
            finally:
                connection.close()

        try:
            processed, inserted = await asyncio.to_thread(import_sync)
        except Exception as exc:
            LOGGER.exception("Upload import failed filename=%s", job.original_filename)
            await self._edit_status(job, f"❌ خطا در ایمپورت {job.original_filename}\n{type(exc).__name__}: {exc}\nفایل روی سرور نگه داشته شد.")
            return

        if self.settings.delete_csv_after_import:
            LOGGER.warning(
                "DELETE_CSV_AFTER_IMPORT is ignored: source CSV is required for "
                "offset-backed result hydration path=%s",
                job.path,
            )
        await self._edit_status(
            job,
            f"✅ ایمپورت کامل شد\n{job.original_filename}\n"
            f"رکوردهای خوانده‌شده: {processed:,}\n"
            f"رکوردهای درج‌شده: {inserted:,}\n"
            "فایل اصلی به‌عنوان منبع داده نگه داشته شد.",
        )


def _format_bytes(value: float) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{value:.1f} B"


def safe_upload_path(data_directory: Path, original_filename: str) -> Path:
    """Create a non-colliding .part path inside the configured import directory."""
    name = Path(original_filename).name
    name = _SAFE_NAME.sub("_", name).strip("._") or "upload.csv"
    if Path(name).suffix.lower() not in {".csv", ".txt"}:
        name += ".csv"
    return data_directory / f".telegram-{secrets.token_hex(10)}-{name}.part"
