from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bot
from telegram.error import TimedOut


class FakeStatus:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str) -> None:
        self.edits.append(text)


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, dict]] = []
        self.status = FakeStatus()

    async def reply_text(self, text: str, **kwargs):
        self.replies.append((text, kwargs))
        return self.status


class FakeBot:
    def __init__(self) -> None:
        self.documents: list[dict] = []

    async def send_document(self, **kwargs):
        document = kwargs["document"]
        self.documents.append({
            "filename": kwargs["filename"],
            "caption": kwargs["caption"],
            "read_timeout": kwargs["read_timeout"],
            "write_timeout": kwargs["write_timeout"],
            "content": document.read(),
        })
        return SimpleNamespace(message_id=10)


class ResultDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.message = FakeMessage()
        self.fake_bot = FakeBot()
        self.update = SimpleNamespace(
            effective_message=self.message,
            effective_chat=SimpleNamespace(type="private", id=123),
            effective_user=SimpleNamespace(id=456),
        )
        self.context = SimpleNamespace(bot=self.fake_bot)
        self.settings = SimpleNamespace(max_query_length=160)

    def _exporter(self, settings, query, destination, aliases, *, filename_prefix, **kwargs):
        path = Path(destination) / f"{filename_prefix}.txt"
        path.write_text("━━━━━━━━━━━━━━━━━━\nFULL_NAME: مجید\n", encoding="utf-8")
        return [path]

    async def test_more_than_five_sends_only_named_txt_with_count_and_30s_timeout(self) -> None:
        result = SimpleNamespace(total_count=6, mode="text", rows=[])
        with (
            patch("bot.run_search", return_value=result),
            patch("bot.load_display_header_aliases", return_value={}),
            patch("bot.write_search_text_exports", side_effect=self._exporter),
            patch("bot.format_search_preview") as preview,
        ):
            await bot.respond_to_search(self.update, self.context, self.settings, "مجید")

        preview.assert_not_called()
        self.assertEqual(len(self.message.replies), 1)
        self.assertIn("در حال آماده‌سازی", self.message.replies[0][0])
        self.assertEqual(len(self.fake_bot.documents), 1)
        sent = self.fake_bot.documents[0]
        self.assertEqual(sent["filename"], "نتایج_مجید.txt")
        self.assertIn("تعداد کل رکوردهای یافت‌شده: 6", sent["caption"])
        self.assertEqual(sent["read_timeout"], 30)
        self.assertEqual(sent["write_timeout"], 30)
        self.assertTrue(any("در حال ارسال" in text for text in self.message.status.edits))
        self.assertTrue(any("ارسال همهٔ فایل‌های نتیجه کامل شد" in text for text in self.message.status.edits))

    async def test_five_results_are_shown_in_chat_without_txt_upload(self) -> None:
        result = SimpleNamespace(total_count=5, mode="text", rows=[])
        with (
            patch("bot.run_search", return_value=result),
            patch("bot.load_display_header_aliases", return_value={}),
            patch("bot.format_search_preview", return_value="همهٔ پنج نتیجه"),
            patch("bot.write_search_text_exports") as exporter,
        ):
            await bot.respond_to_search(self.update, self.context, self.settings, "مجید")

        self.assertEqual(self.message.replies[0][0], "همهٔ پنج نتیجه")
        self.assertEqual(len(self.message.replies), 1)
        self.assertEqual(self.fake_bot.documents, [])
        exporter.assert_not_called()

    async def test_timeout_shows_problem_message_after_send_wait_expires(self) -> None:
        result = SimpleNamespace(total_count=9, mode="text", rows=[])

        async def timed_out_send(**kwargs):
            raise TimedOut("Timed out")

        self.fake_bot.send_document = timed_out_send
        with (
            patch("bot.run_search", return_value=result),
            patch("bot.load_display_header_aliases", return_value={}),
            patch("bot.write_search_text_exports", side_effect=self._exporter),
        ):
            await bot.respond_to_search(self.update, self.context, self.settings, "مجید")

        self.assertTrue(any("در حال ارسال" in text for text in self.message.status.edits))
        self.assertTrue(any("ارسال فایل تا ۳۰ ثانیه کامل نشد" in text for text in self.message.status.edits))

    async def test_wait_for_cancels_a_stalled_send_after_configured_deadline(self) -> None:
        result = SimpleNamespace(total_count=9, mode="text", rows=[])

        async def stalled_send(**kwargs):
            await asyncio.sleep(1)

        self.fake_bot.send_document = stalled_send
        with (
            patch("bot.TEXT_SEND_TIMEOUT_SECONDS", 0.01),
            patch("bot.run_search", return_value=result),
            patch("bot.load_display_header_aliases", return_value={}),
            patch("bot.write_search_text_exports", side_effect=self._exporter),
        ):
            await bot.respond_to_search(self.update, self.context, self.settings, "مجید")

        self.assertTrue(any("در حال ارسال" in text for text in self.message.status.edits))
        self.assertTrue(any("ارسال فایل تا ۳۰ ثانیه کامل نشد" in text for text in self.message.status.edits))


if __name__ == "__main__":
    unittest.main()
