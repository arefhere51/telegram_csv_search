"""FIFO single-worker queue for user search requests."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import logging
from threading import Event
import time
import uuid
from typing import Any, Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from progress_utils import progress_text

LOGGER = logging.getLogger("telegram_csv_search")


@dataclass(slots=True)
class SearchRequest:
    request_id: str
    update: Any
    context: Any
    query: str
    user_id: int
    status_message: Any
    cancel_event: Event = field(default_factory=Event)
    enqueued_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None


SearchProcessor = Callable[..., Awaitable[None]]


class SearchQueue:
    """Run one search at a time, in FIFO order, across all users."""

    def __init__(self, processor: SearchProcessor):
        self.processor = processor
        self._queue: deque[SearchRequest] = deque()
        self._pending_by_user: dict[int, SearchRequest] = {}
        self._active: SearchRequest | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._stopping = False
        self._durations: deque[float] = deque(maxlen=50)

    def is_user_busy(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return bool(
            (self._active and self._active.user_id == user_id)
            or user_id in self._pending_by_user
        )

    def is_user_cancelling(self, user_id: int | None) -> bool:
        """Return whether the user's active search was signalled for cancellation."""
        return bool(
            user_id is not None
            and self._active
            and self._active.user_id == user_id
            and self._active.cancel_event.is_set()
        )

    @property
    def is_busy(self) -> bool:
        return self._active is not None or bool(self._queue)

    async def submit(self, update: Any, context: Any, query: str) -> bool:
        user = update.effective_user
        message = update.effective_message
        if user is None or message is None:
            return False
        user_id = int(user.id)
        settings = context.application.bot_data["settings"]
        query = query.strip()
        if not query:
            await message.reply_text("یک شماره، شناسه، نام یا عبارت برای جست‌وجو بفرستید.")
            return False
        if len(query) > settings.max_query_length:
            await message.reply_text(
                f"عبارت جست‌وجو بیش از حد طولانی است؛ حداکثر {settings.max_query_length} نویسه مجاز است."
            )
            return False
        if self.is_user_busy(user_id):
            if self.is_user_cancelling(user_id):
                await message.reply_text(
                    "لغو جست‌وجوی قبلی هنوز کامل نشده است؛ این درخواست ثبت یا وارد صف نشد. "
                    "بعد از پایان لغو، دوباره جست‌وجو کنید."
                )
            else:
                await message.reply_text(
                    "در حال پردازش درخواست قبلی شما هستم؛ درخواست جدید ثبت نشد. "
                    "برای لغو جست‌وجوهای خودتان و شروع دوباره، /start را بزنید."
                )
            LOGGER.info("operation=search_queue stage=duplicate_rejected user_id=%s", user_id)
            return False

        position = len(self._queue) + (1 if self._active else 0) + 1
        request_id = uuid.uuid4().hex[:16]
        if self.is_busy:
            status_message = await message.reply_text(
                f"⏳ درخواست جست‌وجوی شما در صف انتظار قرار گرفت\nجایگاه فعلی: {position}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "❌ لغو جست‌وجو", callback_data=f"q:cancel:{request_id}"
                )]]),
            )
        else:
            status_message = await message.reply_text(
                progress_text("🔎 جست‌وجو", "در حال آماده‌سازی برای شروع", 0),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                    "❌ لغو جست‌وجو", callback_data=f"q:cancel:{request_id}"
                )]]),
            )
        request = SearchRequest(request_id, update, context, query, user_id, status_message)
        self._queue.append(request)
        self._pending_by_user[user_id] = request
        LOGGER.info(
            "operation=search_queue stage=enqueued user_id=%s position=%d waiting=%d",
            user_id, position, len(self._queue),
        )
        self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        if self._stopping:
            return
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._run(), name="search-request-queue")

    async def _run(self) -> None:
        try:
            while self._queue and not self._stopping:
                request = self._queue.popleft()
                self._pending_by_user.pop(request.user_id, None)
                if request.cancel_event.is_set():
                    continue
                self._active = request
                request.started_at = time.monotonic()
                LOGGER.info(
                    "operation=search_queue stage=started user_id=%s waiting=%d",
                    request.user_id, len(self._queue),
                )
                try:
                    await self.processor(
                        request.update,
                        request.context,
                        request.query,
                        status_message=request.status_message,
                        cancellation_event=request.cancel_event,
                    )
                except asyncio.CancelledError:
                    request.cancel_event.set()
                    raise
                except Exception:
                    LOGGER.exception("operation=search_queue stage=worker_failed user_id=%s", request.user_id)
                    try:
                        await request.status_message.edit_text(
                            "❌ اجرای درخواست جست‌وجو ناموفق شد؛ لطفاً بعداً دوباره تلاش کنید."
                        )
                    except Exception:
                        LOGGER.exception("operation=search_queue stage=error_message_failed user_id=%s", request.user_id)
                finally:
                    if request.started_at is not None:
                        self._durations.append(time.monotonic() - request.started_at)
                    self._active = None
                    LOGGER.info("operation=search_queue stage=completed user_id=%s", request.user_id)
        finally:
            self._worker_task = None
            if self._queue and not self._stopping:
                self._ensure_worker()

    async def cancel_user(self, user_id: int) -> tuple[int, bool]:
        """Cancel this user's queued searches and signal their running search."""
        kept: deque[SearchRequest] = deque()
        cancelled: list[SearchRequest] = []
        cancelled_waiting = 0
        while self._queue:
            request = self._queue.popleft()
            if request.user_id != user_id:
                kept.append(request)
                continue
            request.cancel_event.set()
            if self._pending_by_user.get(user_id) is request:
                self._pending_by_user.pop(user_id, None)
            cancelled_waiting += 1
            cancelled.append(request)
        self._queue = kept
        active_cancel_requested = bool(self._active and self._active.user_id == user_id)
        if active_cancel_requested:
            self._active.cancel_event.set()
        # Finish the queue mutation before awaiting Telegram so concurrent updates
        # cannot be overwritten by a newly enqueued request.
        for request in cancelled:
            try:
                await request.status_message.edit_text(progress_text(
                    "⚪ جست‌وجو لغو شد",
                    "درخواست منتظر با دستور /start لغو شد",
                    100,
                ))
            except Exception:
                LOGGER.exception("operation=search_queue stage=queued_cancel_message_failed user_id=%s", user_id)
        if active_cancel_requested:
            LOGGER.info("operation=search_queue stage=active_cancel_requested user_id=%s", user_id)
        LOGGER.info(
            "operation=search_queue stage=user_queue_cleared user_id=%s cancelled_waiting=%d active_cancel_requested=%s",
            user_id, cancelled_waiting, active_cancel_requested,
        )
        return cancelled_waiting, active_cancel_requested

    def cancel_request(self, request_id: str, user_id: int) -> str:
        for request in list(self._queue):
            if request.request_id == request_id:
                if request.user_id != user_id:
                    return "unauthorized"
                self._queue.remove(request)
                self._pending_by_user.pop(user_id, None)
                request.cancel_event.set()
                return "queued"
        if self._active and self._active.request_id == request_id:
            if self._active.user_id != user_id:
                return "unauthorized"
            self._active.cancel_event.set()
            return "active"
        return "missing"

    def estimated_wait_seconds(self, request: SearchRequest) -> int:
        average = sum(self._durations) / len(self._durations) if self._durations else 0
        ahead = list(self._queue).index(request) if request in self._queue else 0
        return int(max(0, average * (ahead + (1 if self._active else 0))))

    async def stop(self) -> None:
        self._stopping = True
        for request in self._queue:
            request.cancel_event.set()
        self._queue.clear()
        self._pending_by_user.clear()
        if self._active:
            self._active.cancel_event.set()
        task = self._worker_task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._worker_task = None
