from __future__ import annotations

import logging
import re
import sys
import unittest

from logging_utils import RedactionFilter
from progress_utils import new_operation_id, progress_bar, progress_text
from upload_manager import UploadManager


class ProgressObservabilityTests(unittest.TestCase):
    def test_operation_id_is_short_and_searchable(self) -> None:
        operation_id = new_operation_id()
        self.assertRegex(operation_id, re.compile(r"^[A-F0-9]{8}$"))

    def test_progress_bar_covers_zero_and_one_hundred(self) -> None:
        self.assertEqual(progress_bar(0), "[" + "░" * 18 + "]")
        self.assertEqual(progress_bar(100), "[" + "█" * 18 + "]")
        text = progress_text("عملیات", "در حال تست", 50, operation_id="AB12CD34")
        self.assertIn("50.0%", text)
        self.assertIn("کد پیگیری: AB12CD34", text)

    def test_log_filter_redacts_secrets_from_message_and_traceback(self) -> None:
        secret = "bot-token-secret"
        try:
            raise RuntimeError(f"request failed at https://example.test/{secret}")
        except RuntimeError:
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname=__file__, lineno=1,
                msg=f"failed token={secret}", args=(), exc_info=sys.exc_info(),
            )
        self.assertTrue(RedactionFilter((secret,)).filter(record))
        self.assertNotIn(secret, record.getMessage())
        self.assertIsNone(record.exc_info)
        self.assertIn("[REDACTED]", record.exc_text or "")
        self.assertNotIn(secret, record.exc_text or "")

    def test_import_progress_text_contains_percentage_and_operation_id(self) -> None:
        manager = object.__new__(UploadManager)
        text = manager._format_import(
            "File: source.csv; rows_read: 25; rows_inserted: 25; progress: 37.5%",
            "A1B2C3D4",
        )
        self.assertIn("37.5%", text)
        self.assertIn("A1B2C3D4", text)
        self.assertIn("خواندن CSV", text)


if __name__ == "__main__":
    unittest.main()
