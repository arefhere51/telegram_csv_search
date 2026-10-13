from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from bot import (
    BotInstanceAlreadyRunning,
    acquire_instance_lock,
    upload_selection_keyboard,
    upload_selection_prompt,
)


class BotRuntimeSafetyTests(unittest.TestCase):
    def test_second_instance_on_same_host_is_refused_by_lock(self) -> None:
        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / "data" / ".telegram-csv-search.lock"
            first = acquire_instance_lock(lock_path)
            try:
                with self.assertRaises(BotInstanceAlreadyRunning):
                    acquire_instance_lock(lock_path)
            finally:
                first.close()

            second = acquire_instance_lock(lock_path)
            second.close()

    def test_header_picker_shows_page_count_and_next_button_first(self) -> None:
        session = SimpleNamespace(
            token="test-token",
            page=0,
            headers=tuple(f"header_{index}" for index in range(10)),
            selected=set(),
            job=SimpleNamespace(original_filename="large.csv"),
        )

        first_page_text = upload_selection_prompt(session)
        first_page_keyboard = upload_selection_keyboard(session).inline_keyboard
        self.assertIn("10 ستون", first_page_text)
        self.assertIn("صفحهٔ 1 از 2", first_page_text)
        self.assertIn("«بعدی →»", first_page_text)
        self.assertEqual(first_page_keyboard[0][0].text, "بعدی →")
        self.assertEqual(len(first_page_keyboard[1:-1]), 8)

        session.page = 1
        second_page_text = upload_selection_prompt(session)
        second_page_keyboard = upload_selection_keyboard(session).inline_keyboard
        self.assertIn("صفحهٔ 2 از 2", second_page_text)
        self.assertEqual(second_page_keyboard[0][0].text, "← قبلی")
        self.assertEqual(len(second_page_keyboard[1:-1]), 2)


if __name__ == "__main__":
    unittest.main()
