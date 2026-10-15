from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram.ext import ApplicationHandlerStop
from bot import process_queued_search, search_busy_guard
from search_queue import SearchQueue


class FakeStatusMessage:
    def __init__(self, text=""):
        self.text = text
        self.edits = []

    async def edit_text(self, text, **kwargs):
        self.text = text
        self.edits.append(text)
        return self


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        status = FakeStatusMessage(text)
        self.replies.append(status)
        return status


def make_update(user_id: int):
    message = FakeMessage()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        callback_query=None,
    )
    return update, message


def make_context():
    settings = SimpleNamespace(max_query_length=100)
    application = SimpleNamespace(bot_data={"settings": settings})
    return SimpleNamespace(application=application, user_data={})


class SearchQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_adapter_passes_settings_and_raw_query_to_search_handler(self):
        context = make_context()
        update, message = make_update(100)
        called = asyncio.Event()

        async def fake_respond(*args, **kwargs):
            called.set()

        queue = SearchQueue(process_queued_search)
        with patch("bot.respond_to_search", new_callable=AsyncMock, side_effect=fake_respond) as responder:
            try:
                await queue.submit(update, context, "Ali")
                await asyncio.wait_for(called.wait(), timeout=1)
                self.assertEqual(responder.await_count, 1)
                args = responder.await_args.args
                kwargs = responder.await_args.kwargs
                self.assertEqual(args, (update, context, context.application.bot_data["settings"], "Ali"))
                self.assertIs(kwargs["status_message"], message.replies[0])
                self.assertIsInstance(kwargs["cancellation_event"], Event)
            finally:
                await queue.stop()

    async def test_fifo_queue_duplicate_rejection_and_queue_position(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_finished = asyncio.Event()
        processed = []

        async def processor(update, context, query, *, status_message, cancellation_event):
            processed.append((update.effective_user.id, query))
            if query == "first":
                first_started.set()
                await release_first.wait()
            else:
                second_finished.set()

        queue = SearchQueue(processor)
        context = make_context()
        first_update, first_message = make_update(101)
        second_update, second_message = make_update(202)

        try:
            await queue.submit(first_update, context, "first")
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await queue.submit(second_update, context, "second")
            self.assertIn("جایگاه فعلی: 2", second_message.replies[0].text)
            self.assertTrue(queue.is_user_busy(202))

            await queue.submit(first_update, context, "duplicate")
            self.assertIn("درخواست قبلی", first_message.replies[-1].text)
            self.assertEqual(len(processed), 1)

            release_first.set()
            await asyncio.wait_for(second_finished.wait(), timeout=1)
            self.assertEqual(processed, [(101, "first"), (202, "second")])
        finally:
            release_first.set()
            await queue.stop()

    async def test_start_cancels_users_waiting_searches_and_signals_active_search(self):
        active_started = asyncio.Event()
        second_started = asyncio.Event()
        active_cancelled = asyncio.Event()

        async def processor(update, context, query, *, status_message, cancellation_event):
            if query == "active":
                active_started.set()
                while not cancellation_event.is_set():
                    await asyncio.sleep(0.005)
                active_cancelled.set()
            else:
                second_started.set()

        queue = SearchQueue(processor)
        context = make_context()
        user_update, _ = make_update(303)
        other_update, _ = make_update(404)

        try:
            await queue.submit(user_update, context, "active")
            await asyncio.wait_for(active_started.wait(), timeout=1)
            await queue.submit(other_update, context, "other")

            cancelled_waiting, active_cancel_requested = await queue.cancel_user(303)
            self.assertEqual(cancelled_waiting, 0)
            self.assertTrue(active_cancel_requested)
            await asyncio.wait_for(active_cancelled.wait(), timeout=1)
            await asyncio.wait_for(second_started.wait(), timeout=1)
        finally:
            await queue.stop()

    async def test_start_removes_users_pending_search_without_cancelling_active_other_user(self):
        active_started = asyncio.Event()
        release_active = asyncio.Event()
        processed = []

        async def processor(update, context, query, *, status_message, cancellation_event):
            processed.append((update.effective_user.id, query))
            if query == "active":
                active_started.set()
                await release_active.wait()

        queue = SearchQueue(processor)
        context = make_context()
        active_update, _ = make_update(505)
        waiting_update, waiting_message = make_update(606)

        try:
            await queue.submit(active_update, context, "active")
            await asyncio.wait_for(active_started.wait(), timeout=1)
            await queue.submit(waiting_update, context, "waiting")
            cancelled_waiting, active_cancel_requested = await queue.cancel_user(606)
            self.assertEqual(cancelled_waiting, 1)
            self.assertFalse(active_cancel_requested)
            self.assertIn("جست‌وجو لغو شد", waiting_message.replies[0].text)
            release_active.set()
            await asyncio.sleep(0.02)
            self.assertEqual(processed, [(505, "active")])
        finally:
            release_active.set()
            await queue.stop()

    async def test_busy_user_guard_rejects_second_command_but_allows_start(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def processor(update, context, query, *, status_message, cancellation_event):
            started.set()
            await release.wait()

        queue = SearchQueue(processor)
        context = make_context()
        context.application.bot_data["search_queue"] = queue
        first_update, _ = make_update(707)
        try:
            await queue.submit(first_update, context, "first")
            await asyncio.wait_for(started.wait(), timeout=1)

            second_update, second_message = make_update(707)
            second_update.effective_message.text = "/stats"
            with self.assertRaises(ApplicationHandlerStop):
                await search_busy_guard(second_update, context)
            self.assertIn("درخواست قبلی", second_message.replies[0].text)

            start_update, _ = make_update(707)
            start_update.effective_message.text = "/start"
            await search_busy_guard(start_update, context)
        finally:
            release.set()
            await queue.stop()


if __name__ == "__main__":
    unittest.main()
