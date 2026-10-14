from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from finderbot_manager import cleanup_plan, delete_all_bot_data


class FinderBotManagerCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "telegram_csv_search"
        self.root.mkdir()
        self.data = self.root / "data"
        self.database_dir = self.root / "database"
        self.logs = self.root / "logs"
        self.data.mkdir()
        self.database_dir.mkdir()
        self.logs.mkdir()
        self.settings = SimpleNamespace(
            data_directory=self.data,
            database_path=self.database_dir / "search.db",
            log_directory=self.logs,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plan_and_cleanup_remove_bot_data_but_preserve_project_and_configuration(self) -> None:
        (self.root / ".env").write_text("TELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
        (self.root / "bot.py").write_text("# project source\n", encoding="utf-8")
        (self.root / "venv.txt").write_text("virtualenv placeholder\n", encoding="utf-8")
        (self.data / "downloaded.csv").write_text("name\nAli\n", encoding="utf-8")
        (self.data / ".telegram-csv-search.lock").write_text("123\n", encoding="ascii")
        (self.database_dir / "search.db").write_bytes(b"sqlite")
        (self.database_dir / "search.db-wal").write_bytes(b"wal")
        (self.database_dir / "older.sqlite3").write_bytes(b"sqlite")
        (self.logs / "bot.log").write_text("log\n", encoding="utf-8")
        run_directory = self.root / "run"
        run_directory.mkdir()
        (run_directory / "finderbot.pid").write_text("999999\n", encoding="ascii")
        archive = self.root / "data_deleted_sources" / "file_9"
        archive.mkdir(parents=True)
        (archive / "archived.csv").write_text("name\n", encoding="utf-8")

        plan = cleanup_plan(self.settings, self.root)
        self.assertTrue(any("search.db" in item for item in plan))
        self.assertFalse(any(item.endswith(".env") for item in plan))
        removed = delete_all_bot_data(self.settings, self.root)

        self.assertGreaterEqual(len(removed), 7)
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertEqual(list(self.logs.iterdir()), [])
        self.assertFalse((self.database_dir / "search.db").exists())
        self.assertFalse((self.database_dir / "search.db-wal").exists())
        self.assertFalse((self.database_dir / "older.sqlite3").exists())
        self.assertFalse((self.root / "data_deleted_sources").exists())
        self.assertFalse((run_directory / "finderbot.pid").exists())
        self.assertFalse(run_directory.exists())
        self.assertEqual((self.root / ".env").read_text(encoding="utf-8"), "TELEGRAM_BOT_TOKEN=secret\n")
        self.assertTrue((self.root / "bot.py").exists())
        self.assertTrue((self.root / "venv.txt").exists())

    def test_cleanup_refuses_to_empty_the_project_root(self) -> None:
        unsafe = SimpleNamespace(
            data_directory=self.root,
            database_path=self.database_dir / "search.db",
            log_directory=self.logs,
        )
        with self.assertRaisesRegex(ValueError, "unsafe data directory"):
            cleanup_plan(unsafe, self.root)
        with self.assertRaisesRegex(ValueError, "unsafe data directory"):
            delete_all_bot_data(unsafe, self.root)


if __name__ == "__main__":
    unittest.main()
