"""Private-chat Telegram document ingestion with live progress updates."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import secrets
import time
from typing import Any, Callable

from telegram import Bot
from telegram.error import BadRequest

from config import Settings
from database import build_search_indexes, connect, initialize_database, seed_dedupe_hashes
from import_csv import import_one_file
from progress_utils import RemainingTimeEstimator, progress_text

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
    source_kind: str = "telegram"


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
            f"⬇️ دریافت فایل\n{filename}\n{progress}\n"
            f"حجم: {size}\nسرعت: {_format_bytes(speed)} در ثانیه"
        )

    def _format_import(self, message: str) -> str:
        # import_csv emits a stable human-readable line with progress, rows and speed.
        match = re.search(r"پیشرفت:\s*([0-9]+(?:\.[0-9]+)?)%", message)
        percent = float(match.group(1)) if match else 0.0
        return progress_text(
            "📥 ورود اطلاعات فایل",
            "خواندن CSV و ثبت ردیف‌ها",
            percent,
            detail=message,
        )

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
                LOGGER.exception(
                    "operation=upload stage=worker_unhandled filename=%s",
                    job.original_filename,
                )
                await self._edit_status(
                    job,
                    progress_text(
                        "❌ پردازش فایل ناموفق بود",
                        "خطای مدیریت‌نشده؛ جزئیات در CMD ثبت شد",
                        100,
                        detail=f"{job.original_filename}؛ فایل روی سرور نگه داشته شد.",
                    ),
                )
            finally:
                self.queue.task_done()

    async def _process(self, job: UploadJob, position: int) -> None:
        LOGGER.info(
            "operation=upload stage=queued user_id=%s filename=%s queue_position=%d bytes=%s",
            job.uploader_id, job.original_filename, position, job.file_size,
        )
        await self._edit_status(
            job,
            progress_text(
                "📥 آماده‌سازی فایل",
                f"در صف ورود اطلاعات؛ موقعیت {position}",
                0,
                detail=job.original_filename,
            ),
        )
        loop = asyncio.get_running_loop()
        last_status = 0.0
        pending_status_updates: list[tuple[str, Any]] = []
        index_eta: dict[str, tuple[RemainingTimeEstimator, float, float]] = {}

        def index_stage_eta(stage: str, percent: float) -> str:
            """Estimate only within the current index substage, never reuse CSV speed."""
            if "انتظار برای آزادشدن" in stage or "قفل موقت" in stage:
                return "تا آزادشدن قفل SQLite قابل برآورد نیست"
            if "قفل نوشتن گرفته شد" in stage:
                return "اندازه‌گیری زمان FTS آغاز می‌شود"
            if "FTS" in stage:
                key, start, end = "fts_rebuild", 5.0, 52.0
                completed = max(0.0, min(percent - start, end - start))
                estimator = index_eta.setdefault(key, (RemainingTimeEstimator(), start, end))[0]
                return estimator.estimate(
                    completed, end - start,
                    finished=("کامل شد" in stage or percent >= end),
                )
            if stage.startswith("ساخت شاخص "):
                name = stage.removeprefix("ساخت شاخص ")
                saved = index_eta.get(name)
                if saved is None:
                    start = percent
                    end = min(start + 6.0, 100.0)
                    saved = (RemainingTimeEstimator(), start, end)
                    index_eta[name] = saved
                estimator, start, end = saved
                if percent >= start + (end - start) * 0.95:
                    return "نزدیک پایان؛ شاخص‌سازی هنوز در حال اجراست"
                return estimator.estimate(
                    max(0.0, min(percent - start, end - start)), end - start
                )
            if stage.startswith("شاخص ") and " ساخته شد" in stage:
                name = stage.removeprefix("شاخص ").removesuffix(" ساخته شد")
                saved = index_eta.get(name)
                if saved is None:
                    return "تکمیل شد"
                estimator, start, end = saved
                return estimator.estimate(end - start, end - start, finished=True)
            if "بازسازی نهایی شاخص‌های B-tree" in stage or "بازسازی نهایی B-tree" in stage:
                key, start, end = "btree_reindex", 90.0, 97.0
                estimator = index_eta.setdefault(key, (RemainingTimeEstimator(), start, end))[0]
                return estimator.estimate(
                    max(0.0, min(percent - start, end - start)), end - start,
                    finished=("کامل" in stage or percent >= end),
                )
            if (
                "ساخت همهٔ شاخص‌ها کامل شد" in stage
                or "شاخص‌ها از قبل آماده‌اند" in stage
                or "شاخص‌ها توسط فرایند دیگری آماده شدند" in stage
            ):
                return "تکمیل شد"
            return "در حال برآورد بر اساس سرعت همین مرحله"

        async def flush_progress_updates() -> None:
            pending = list(pending_status_updates)
            pending_status_updates.clear()
            if not pending:
                return
            results = await asyncio.gather(
                *(asyncio.wrap_future(future) for _, future in pending),
                return_exceptions=True,
            )
            for (stage_name, _), result in zip(pending, results):
                if isinstance(result, BaseException):
                    LOGGER.error(
                        "operation=upload stage=progress_update_failed progress_stage=%s error_type=%s",
                        stage_name, type(result).__name__,
                    )

        def progress(message: str) -> None:
            nonlocal last_status
            now = time.monotonic()
            if now - last_status < self.settings.upload_progress_interval_seconds:
                return
            last_status = now
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._edit_status(job, self._format_import(message)), loop
                )
            except RuntimeError:
                LOGGER.exception("operation=upload stage=import_progress_loop_unavailable")
                return
            pending_status_updates.append(("csv_import_progress", future))

        def index_progress(stage: str, percent: float) -> None:
            nonlocal last_status
            if stage.startswith("ساخت شاخص "):
                stage_code = "btree_index_build"
                index_name = stage.removeprefix("ساخت شاخص ")
            elif stage.startswith("شاخص ") and " ساخته شد" in stage:
                stage_code = "btree_index_complete"
                index_name = stage.removeprefix("شاخص ").removesuffix(" ساخته شد")
            elif "FTS" in stage:
                stage_code = "fts_rebuild"
                index_name = "records_all_fts"
            elif "قفل" in stage:
                stage_code = "sqlite_lock_wait"
                index_name = "-"
            elif "کامل" in stage or "آماده" in stage:
                stage_code = "index_build_complete"
                index_name = "-"
            else:
                stage_code = "index_preparation"
                index_name = "-"
            eta_text = index_stage_eta(stage, percent)
            now = time.monotonic()
            terminal = (
                "ساخته شد" in stage
                or "کامل شد" in stage
                or "آماده‌اند" in stage
                or "آماده شدند" in stage
            )
            if now - last_status < self.settings.upload_progress_interval_seconds and not terminal:
                return
            last_status = now
            LOGGER.info(
                "operation=upload stage=%s index=%s progress=%.1f filename=%s",
                stage_code, index_name, percent, job.original_filename,
            )
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._edit_status(
                        job,
                        progress_text(
                            "🧱 آماده‌سازی شاخص جست‌وجو",
                            stage,
                            percent,
                            detail=(
                                f"{job.original_filename}\n"
                                f"زمان تقریبی باقی‌مانده برای این مرحله: {eta_text}"
                            ),
                        ),
                    ),
                    loop,
                )
            except RuntimeError:
                LOGGER.exception("operation=upload stage=index_progress_loop_unavailable")
                return
            pending_status_updates.append((stage_code, future))

        metadata = None
        if job.source_kind == "telegram":
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
                return processed, inserted
            finally:
                connection.close()

        def build_indexes_sync() -> None:
            connection = connect(self.settings.database_path)
            try:
                initialize_database(connection)
                build_search_indexes(connection, progress_callback=index_progress)
            finally:
                connection.close()

        try:
            LOGGER.info(
                "operation=upload stage=csv_import_started user_id=%s filename=%s source=%s bytes=%s",
                job.uploader_id, job.original_filename, job.source_kind, job.file_size,
            )
            processed, inserted = await asyncio.to_thread(import_sync)
            await flush_progress_updates()
        except Exception as exc:
            await flush_progress_updates()
            LOGGER.exception(
                "operation=upload stage=csv_import_failed filename=%s error_type=%s",
                job.original_filename, type(exc).__name__,
            )
            await self._edit_status(
                job,
                progress_text(
                    "❌ ورود اطلاعات ناموفق بود",
                    "خطا هنگام خواندن CSV یا ثبت ردیف‌ها",
                    100,
                    detail="جزئیات فنی در گزارش سرور ثبت شد؛ فایل روی سرور نگه داشته شد.",
                ),
            )
            return

        LOGGER.info(
            "operation=upload stage=csv_import_complete filename=%s rows_read=%d rows_inserted=%d",
            job.original_filename, processed, inserted,
        )
        await self._edit_status(
            job,
            progress_text(
                "⏳ ورود ردیف‌ها کامل شد",
                "شروع آماده‌سازی شاخص جست‌وجو",
                0,
                detail=job.original_filename,
            ),
        )
        try:
            await asyncio.to_thread(build_indexes_sync)
            await flush_progress_updates()
        except Exception:
            await flush_progress_updates()
            LOGGER.exception(
                "operation=upload stage=search_index_failed filename=%s rows=%d",
                job.original_filename, processed,
            )
            await self._edit_status(
                job,
                progress_text(
                    "⚠️ ورود ردیف‌ها کامل شد، اما شاخص‌سازی متوقف شد",
                    "خطای فنی در گزارش سرور ثبت شده است.",
                    100,
                    detail=(
                        f"ردیف‌های فایل {job.original_filename} وارد شدند، اما مرحلهٔ شاخص‌سازی کامل نشد. "
                        "فایل را دوباره نفرستید؛ مرحلهٔ شاخص‌سازی را بررسی کنید."
                    ),
                ),
            )
            return

        LOGGER.info(
            "operation=upload stage=complete filename=%s rows_read=%d rows_inserted=%d",
            job.original_filename, processed, inserted,
        )

        if self.settings.delete_csv_after_import:
            LOGGER.warning(
                "DELETE_CSV_AFTER_IMPORT is ignored: source CSV is required for "
                "offset-backed result hydration path=%s",
                job.path,
            )
        await self._edit_status(
            job,
            progress_text(
                "✅ ورود اطلاعات کامل شد",
                "همهٔ مرحله‌ها با موفقیت انجام شدند",
                100,
                detail=(
                    f"{job.original_filename}\nرکوردهای خوانده‌شده: {processed:,}\n"
                    f"رکوردهای درج‌شده: {inserted:,}\nفایل اصلی به‌عنوان منبع داده نگه داشته شد.\n"
                    "زمان تقریبی باقی‌مانده: تکمیل شد."
                ),
            ),
        )


def _format_bytes(value: float) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{value:.1f} B"


def safe_upload_path(
    data_directory: Path,
    original_filename: str,
    *,
    source_prefix: str = "telegram",
) -> Path:
    """Create a non-colliding .part path inside the configured import directory."""
    name = Path(original_filename).name
    name = _SAFE_NAME.sub("_", name).strip("._") or "upload.csv"
    if Path(name).suffix.lower() not in {".csv", ".txt"}:
        name += ".csv"
    prefix = _SAFE_NAME.sub("_", source_prefix).strip("._") or "upload"
    return data_directory / f".{prefix}-{secrets.token_hex(10)}-{name}.part"
