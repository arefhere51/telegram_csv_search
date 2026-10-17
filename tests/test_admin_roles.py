from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import Settings
from database import add_bot_user, connect, initialize_access_control, initialize_database
from bot import (
    _admin_bot_commands,
    _admin_section_view,
    _user_bot_commands,
    start_command,
)


class AdminConfigurationTests(unittest.TestCase):
    def _settings_from_env(self, **values):
        env = {
            "TELEGRAM_BOT_TOKEN": "123456789:abcdefghijklmnopqrstuvwxyzABCDEFG",
            "ALLOWED_USER_IDS": "10,20,30",
            "ADMIN_USER_IDS": "20,30",
            "DATA_DIRECTORY": "./data",
            "DATABASE_PATH": "./database/search.db",
            "LOG_DIRECTORY": "./logs",
            "CSV_HAS_HEADER": "true",
            "UPLOAD_ENABLED": "true",
        }
        env.update(values)
        with patch("config.load_dotenv"), patch.dict(os.environ, env, clear=True):
            return Settings.from_env()

    def test_explicit_primary_admin_is_distinct_from_other_existing_admins(self) -> None:
        settings = self._settings_from_env(PRIMARY_ADMIN_USER_IDS="30")
        self.assertEqual(settings.primary_admin_user_ids, frozenset({30}))
        self.assertEqual(settings.admin_user_ids, frozenset({20, 30}))

    def test_existing_installation_defaults_first_admin_to_primary(self) -> None:
        settings = self._settings_from_env(PRIMARY_ADMIN_USER_IDS="")
        self.assertEqual(settings.primary_admin_user_ids, frozenset({20}))
        self.assertEqual(settings.admin_user_ids, frozenset({20, 30}))

    def test_default_command_menu_exposes_no_admin_or_help_commands(self) -> None:
        commands = {item.command for item in _user_bot_commands()}
        self.assertEqual(commands, {"start", "search", "stats"})
        self.assertNotIn("help", commands)
        admin_commands = {item.command for item in _admin_bot_commands(primary_admin=False)}
        self.assertIn("admin", admin_commands)
        self.assertNotIn("admin_add", admin_commands)
        self.assertIn("admin_add", {item.command for item in _admin_bot_commands(primary_admin=True)})

    def test_admin_roles_section_is_not_revealed_to_non_primary_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "search.db"
            connection = connect(db_path)
            initialize_database(connection)
            connection.close()
            settings = SimpleNamespace(
                database_path=db_path,
                allowed_user_ids=frozenset({10, 20}),
                admin_user_ids=frozenset({10, 20}),
                primary_admin_user_ids=frozenset({10}),
            )
            text, _ = _admin_section_view(settings, "admins", primary_admin=False)
            self.assertIn("فقط برای مدیر اصلی", text)
            self.assertNotIn("/admin_add", text)


class StartGuideTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        db_path = Path(self.tempdir.name) / "search.db"
        self.settings = SimpleNamespace(
            database_path=db_path,
            allowed_user_ids=frozenset({101}),
            admin_user_ids=frozenset({101}),
            primary_admin_user_ids=frozenset({101}),
            initial_access_mode="private",
        )
        connection = connect(db_path)
        initialize_database(connection)
        initialize_access_control(connection, self.settings)
        add_bot_user(connection, 202, 101)
        connection.close()

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def _start_text(self, user_id: int) -> str:
        class Message:
            text = ""

            async def reply_text(self, text, **kwargs):
                self.text = text

        class Bot:
            async def set_my_commands(self, *args, **kwargs):
                return True

        message = Message()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id),
            effective_chat=SimpleNamespace(type="private"),
            effective_message=message,
        )
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={"settings": self.settings}),
            bot=Bot(),
            user_data={},
        )
        await start_command(update, context)
        return message.text

    async def test_start_keeps_admin_commands_out_of_ordinary_user_guide(self) -> None:
        text = await self._start_text(202)
        self.assertIn("/search", text)
        self.assertNotIn("/admin", text)
        self.assertNotIn("/database", text)
        self.assertNotIn("/user_add", text)

    async def test_start_shows_admin_panel_only_to_admin(self) -> None:
        text = await self._start_text(101)
        self.assertIn("/admin", text)
        self.assertNotIn("/admin_add", text)


if __name__ == "__main__":
    unittest.main()
