from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from config import Settings
from database import (
    DatabaseRebuildRequired,
    SCHEMA_VERSION,
    access_mode,
    add_bot_admin,
    add_bot_user,
    bot_admin_exists,
    bot_user_exists,
    build_search_indexes,
    connect,
    delete_header_alias,
    get_header_aliases,
    get_hidden_header_keys,
    hide_header,
    initialize_access_control,
    initialize_database,
    list_bot_admins,
    list_bot_users,
    remove_bot_admin,
    remove_bot_user,
    set_access_mode,
    set_header_alias,
    show_header,
)
from import_csv import OffsetCsvReader, import_one_file
from search import parse_raw_fields, search_records
from upload_manager import safe_upload_path
from bot import (
    format_record,
    format_full_text_record,
    format_search_preview,
    is_admin,
    is_primary_admin,
    download_file_with_progress,
    authorized,
    upload_authorized,
    deny_if_unauthorized,
    full_results_csv_bytes,
    page_needs_full_csv,
    telegram_file_download_url,
    write_search_text_exports,
)


class ImportAndSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.data_directory = root / "data"
        self.data_directory.mkdir()
        self.database_path = root / "database" / "search.db"
        shutil.copy(Path(__file__).with_name("sample_records.csv"), self.data_directory / "sample.csv")
        self.settings = Settings(
            telegram_bot_token="unused-in-tests",
            allowed_user_ids=frozenset({1}),
            data_directory=self.data_directory,
            database_path=self.database_path,
            log_directory=root / "logs",
            csv_delimiter=",",
            csv_encoding="utf-8-sig",
            csv_has_header=False,
            csv_column_names=("column_1", "column_2", "name", "birth_date", "phone"),
            duplicate_mode="keep",
            import_batch_size=2,
            import_progress_every_rows=1000,
            max_results=100,
            results_per_page=10,
            max_query_length=160,
            max_field_display_chars=280,
            phone_normalization=True,
            prefix_search_min_length=3,
            log_level="INFO",
            upload_enabled=True,
            upload_max_bytes=3_000_000_000,
            upload_progress_interval_seconds=3,
            delete_csv_after_import=False,
            telegram_base_url="https://api.telegram.org/bot",
            telegram_file_base_url="https://api.telegram.org/file/bot",
        )
        self.connection = connect(self.database_path)
        initialize_database(self.connection)
        import_one_file(self.connection, self.data_directory / "sample.csv", self.settings)
        build_search_indexes(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def test_import_preserves_all_records_and_exact_phone_search(self) -> None:
        total = self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        self.assertEqual(total, 5)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(records)")}
        self.assertNotIn("raw_fields_json", columns)
        self.assertNotIn("name", columns)
        self.assertIn("byte_offset_start", columns)
        self.assertIn("byte_offset_end", columns)
        result = search_records(self.connection, "9359090161", 0, self.settings)
        self.assertEqual(result.mode, "exact")
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.rows[0]["name"], "روح الله مرادلو")

    def test_persian_normalization_and_fts_prefix_search(self) -> None:
        # Arabic kaf/yeh variants and zero-width joins normalize to the stored Persian name.
        result = search_records(self.connection, "روح‌الله", 0, self.settings)
        self.assertEqual(result.mode, "text")
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.rows[0]["column_1"], "66506301")

    def test_text_search_includes_exact_name_and_longer_names_and_exports_all(self) -> None:
        names = (
            "علی",
            "علی امیری",
            "علی حسینی",
            "علی محمدی",
            "علی اکبری",
            "علی رضایی",
        )
        source = self.data_directory / "ali-names.csv"
        source.write_text(
            "".join(f"{name},test-{index}\n" for index, name in enumerate(names)),
            encoding="utf-8",
            newline="",
        )
        import_one_file(self.connection, source, self.settings)
        build_search_indexes(self.connection)

        result = search_records(self.connection, "علی", 0, self.settings)
        self.assertEqual(result.mode, "text")
        self.assertEqual(result.total_count, len(names))
        self.assertEqual({row["column_1"] for row in result.rows}, set(names))

        export_settings = replace(self.settings, results_per_page=2)
        export_dir = self.data_directory / "exports"
        paths = write_search_text_exports(export_settings, "علی", export_dir)
        self.assertTrue(paths)
        exported = "".join(path.read_text(encoding="utf-8") for path in paths)
        for name in names:
            with self.subTest(name=name):
                self.assertIn(f"column_1: {name}", exported)

    def test_identifier_prefix_search_uses_indexable_range_predicates(self) -> None:
        result = search_records(self.connection, "9359", 0, self.settings)
        self.assertEqual(result.mode, "prefix")
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.rows[0]["phone"], "9359090161")

    def test_reimport_replaces_source_without_duplicate_rows_and_updates_fts(self) -> None:
        import_one_file(self.connection, self.data_directory / "sample.csv", self.settings)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 5)
        result = search_records(self.connection, "غلامحسین", 0, self.settings)
        self.assertEqual(result.total_count, 1)

    def test_bot_module_imports_with_supported_telegram_library(self) -> None:
        # Import validates the python-telegram-bot API surface used by the application.
        import bot

        self.assertTrue(callable(bot.main))

    def test_upload_metadata_columns_and_safe_filename(self) -> None:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(imported_files)")}
        self.assertIn("telegram_filename", columns)
        self.assertIn("telegram_file_id", columns)
        path = safe_upload_path(self.data_directory, "../../private data.csv")
        self.assertEqual(path.parent, self.data_directory)
        self.assertTrue(path.name.endswith("private_data.csv.part"))

    def test_small_utf8_sig_multiline_csv_is_hydrated_from_byte_offsets(self) -> None:
        source = self.data_directory / "multiline.csv"
        source.write_text(
            "id,name,phone,address\r\n"
            "1,مریم,09120000000,\"خط اول\r\nخط دوم، تهران\"\r\n"
            "2,علی,09121111111,شیراز\r\n",
            encoding="utf-8-sig",
            newline="",
        )
        import_one_file(self.connection, source, self.settings)

        offsets = self.connection.execute(
            """SELECT r.byte_offset_start, r.byte_offset_end, r.source_line
               FROM records r JOIN imported_files f ON f.id = r.source_file_id
               WHERE f.relative_path = 'multiline.csv' ORDER BY r.id"""
        ).fetchall()
        self.assertEqual(len(offsets), 2)
        self.assertEqual(offsets[0]["source_line"], 2)
        self.assertEqual(offsets[1]["source_line"], 4)
        self.assertLess(offsets[0]["byte_offset_start"], offsets[0]["byte_offset_end"])
        with source.open("rb") as handle:
            handle.seek(offsets[0]["byte_offset_start"])
            raw_record = handle.read(offsets[0]["byte_offset_end"] - offsets[0]["byte_offset_start"])
        self.assertIn("خط دوم".encode("utf-8"), raw_record)

        result = search_records(self.connection, "خط دوم", 0, self.settings)
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.rows[0]["name"], "مریم")
        self.assertEqual(result.rows[0]["source_line"], 2)
        self.assertEqual(parse_raw_fields(result.rows[0])[3], "خط اول\r\nخط دوم، تهران")

    def test_invalid_utf8_byte_is_replaced_and_file_import_completes(self) -> None:
        source = self.data_directory / "invalid-byte.csv"
        source.write_bytes(b"id,name\n1,Alpha\n2,Bad\x99Name\n3,Leila\n")
        processed, inserted = import_one_file(self.connection, source, self.settings)
        build_search_indexes(self.connection)

        self.assertEqual(processed, 3)
        self.assertEqual(inserted, 3)
        result = search_records(self.connection, "Bad", 0, self.settings)
        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.rows[0]["column_2"], "Bad\ufffdName")

    def test_failed_import_cleans_committed_partial_batches(self) -> None:
        source = self.data_directory / "forced-failure.csv"
        source.write_text("partial-a\npartial-b\npartial-c\n", encoding="utf-8")
        original_read_record = OffsetCsvReader.read_record
        calls = 0

        def fail_after_first_batch(reader):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise ValueError("simulated decode failure")
            return original_read_record(reader)

        with patch("import_csv.OffsetCsvReader.read_record", new=fail_after_first_batch):
            with self.assertRaisesRegex(ValueError, "simulated decode failure"):
                import_one_file(self.connection, source, self.settings)

        file_row = self.connection.execute(
            "SELECT id, status FROM imported_files WHERE relative_path = 'forced-failure.csv'"
        ).fetchone()
        self.assertEqual(file_row["status"], "failed")
        self.assertEqual(
            int(self.connection.execute(
                "SELECT COUNT(*) FROM records WHERE source_file_id = ?", (file_row["id"],)
            ).fetchone()[0]),
            0,
        )

    def test_failed_file_rows_are_excluded_from_search(self) -> None:
        source = self.data_directory / "failed-search.csv"
        source.write_text("uniquefailedterm,unused\n", encoding="utf-8")
        import_one_file(self.connection, source, self.settings)
        build_search_indexes(self.connection)
        self.connection.execute(
            "UPDATE imported_files SET status = 'failed' WHERE relative_path = 'failed-search.csv'"
        )
        self.connection.commit()

        result = search_records(self.connection, "uniquefailedterm", 0, self.settings)
        self.assertEqual(result.total_count, 0)

    def test_result_preview_includes_unselected_and_empty_columns(self) -> None:
        row = {
            "headers_json": '["id", "name", "phone", "address"]',
            "raw_fields_json": '["42", "مریم", "09120000000", ""]',
            "source_file": "people.csv",
            "source_line": 2,
        }
        rendered = format_record(row, 1, self.settings)
        self.assertIn("id:", rendered)
        self.assertIn("name:", rendered)
        self.assertIn("phone:", rendered)
        self.assertIn("address:", rendered)
        self.assertIn("09120000000", rendered)
        self.assertIn("address:</b> —", rendered)

    def test_long_field_is_not_truncated_in_preview(self) -> None:
        long_address = "اصفهان " + "خیابان هزارجریب کوی امام جعفر صادق " * 35 + "پلاک ۱۶ انتهای نشانی"
        row = {
            "headers_json": '["address"]',
            "raw_fields_json": json.dumps([long_address], ensure_ascii=False),
            "source_file": "people.csv",
            "source_line": 2,
        }
        rendered = format_record(row, 1, self.settings)
        self.assertIn(long_address, rendered)
        short_preview = format_search_preview(
            "اصفهان",
            SimpleNamespace(total_count=1, mode="text", rows=[row]),
            self.settings,
        )
        self.assertIn(long_address, short_preview)

        very_long_value = "نشانی-" + "کوچه-" * 1000 + "انتهای-کامل"
        huge_row = {
            "headers_json": '["address"]',
            "raw_fields_json": json.dumps([very_long_value], ensure_ascii=False),
            "source_file": "people.csv",
            "source_line": 3,
        }
        huge_result = SimpleNamespace(total_count=1, mode="text", rows=[huge_row])
        huge_preview = format_search_preview("نشانی", huge_result, self.settings)
        self.assertGreater(len(huge_preview), 3900)
        self.assertIn("انتهای-کامل", huge_preview)
        self.assertTrue(page_needs_full_csv(huge_result, self.settings))

    def test_telegram_file_url_handles_relative_and_absolute_paths(self) -> None:
        relative = telegram_file_download_url(self.settings, "documents/file_7.csv")
        self.assertEqual(
            relative,
            "https://api.telegram.org/file/botunused-in-tests/documents/file_7.csv",
        )
        absolute = "https://api.telegram.org/file/botunused-in-tests/documents/file_7.csv"
        self.assertEqual(telegram_file_download_url(self.settings, absolute), absolute)

    def test_global_header_alias_renames_all_files_in_both_text_formats(self) -> None:
        row = {
            "headers_json": '["NATIONAL_CODE", "name"]',
            "raw_fields_json": '["57661928", "مليحه خباز"]',
            "source_file": "people.csv",
            "source_line": 48879,
        }
        set_header_alias(self.connection, "NATIONAL_CODE", "کد ملی")
        aliases = get_header_aliases(self.connection)
        text = format_full_text_record(row, 1, aliases)
        preview = format_record(row, 1, self.settings, aliases)
        self.assertTrue(text.startswith("━━━━━━━━━━━━━━━━━━"))
        self.assertTrue(preview.startswith("━━━━━━━━━━━━━━━━━━"))
        self.assertIn("کد ملی: 57661928", text)
        self.assertIn("کد ملی:", preview)
        self.assertNotIn("NATIONAL_CODE", text)
        self.assertTrue(delete_header_alias(self.connection, "national_code"))
        self.assertFalse(get_header_aliases(self.connection))

    def test_hidden_headers_are_removed_from_message_and_txt_but_can_be_restored(self) -> None:
        row = {
            "headers_json": '["NATIONAL_CODE", "FULL_NAME"]',
            "raw_fields_json": '["57661928", "مليحه خباز"]',
            "source_file": "people.csv",
            "source_line": 2,
        }
        self.assertTrue(hide_header(self.connection, "NATIONAL_CODE"))
        hidden = get_hidden_header_keys(self.connection)
        self.assertIn("national_code", hidden)
        preview = format_record(row, 1, self.settings, hidden_headers=hidden)
        text = format_full_text_record(row, 1, hidden_headers=hidden)
        self.assertNotIn("NATIONAL_CODE", preview)
        self.assertNotIn("57661928", preview)
        self.assertNotIn("NATIONAL_CODE", text)
        self.assertNotIn("57661928", text)
        self.assertTrue(show_header(self.connection, "national_code"))
        self.assertIn(
            "57661928",
            format_full_text_record(row, 1, hidden_headers=get_hidden_header_keys(self.connection)),
        )

    def test_txt_export_contains_all_matches_in_split_files(self) -> None:
        source = self.data_directory / "many.csv"
        rows = ["id,NATIONAL_CODE,FULL_NAME,PHONE\n"]
        rows.extend(f"{i},57{i:06d},نام‌یافته {i},09{i:09d}\n" for i in range(12))
        source.write_text("".join(rows), encoding="utf-8", newline="")
        import_one_file(self.connection, source, self.settings, selected_column_indices=(0, 2))
        build_search_indexes(self.connection)
        aliases = {"national_code": "کد ملی"}
        output_dir = self.data_directory / "exports"
        files = write_search_text_exports(
            self.settings, "نام‌یافته", output_dir, aliases, page_size=4,
            max_chunk_bytes=350, filename_prefix="نتایج_نام‌یافته",
        )
        self.assertGreater(len(files), 1)
        self.assertTrue(files[0].name.startswith("نتایج_نام"))
        contents = [path.read_text(encoding="utf-8") for path in files]
        joined = "".join(contents)
        self.assertGreaterEqual(joined.count("━━━━━━━━━━━━━━━━━━"), 12)
        self.assertEqual(joined.count("منبع: many.csv:"), 12)
        self.assertEqual(joined.count("کد ملی:"), 12)
        self.assertIn("FULL_NAME: نام‌یافته 11", joined)
        self.assertIn("PHONE: 09000000011", joined)
        preview_result = search_records(self.connection, "نام‌یافته", 0, self.settings)
        preview = format_search_preview("نام‌یافته", preview_result, self.settings, aliases)
        self.assertIn("نام‌یافته 4", preview)
        self.assertNotIn("نام‌یافته 5", preview)

        capped_dir = self.data_directory / "exports-capped"
        capped = write_search_text_exports(
            self.settings,
            "نام‌یافته",
            capped_dir,
            aliases,
            page_size=4,
            max_results=5,
            filename_prefix="first_five",
        )
        capped_text = "".join(path.read_text(encoding="utf-8") for path in capped)
        self.assertEqual(capped_text.count("━━━━━━━━━━━━━━━━━━"), 5)
        self.assertEqual(capped_text.count("منبع: many.csv:"), 5)

    def test_sensitive_search_commands_are_blocked_in_group_chats(self) -> None:
        class FakeMessage:
            text = None
            async def reply_text(self, text):
                self.text = text
        message = FakeMessage()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(type="group"),
            effective_message=message,
            effective_user=SimpleNamespace(id=1),
        )
        blocked = asyncio.run(deny_if_unauthorized(update, self.settings))
        self.assertTrue(blocked)
        self.assertIn("گفت‌وگوی خصوصی", message.text)

    def test_local_bot_api_absolute_path_is_copied_without_http(self) -> None:
        source = self.data_directory / "local-api-file.csv"
        source.write_bytes(b"a,b\n1,2\n")
        destination = self.data_directory / "downloaded.csv.part"

        class FakeBot:
            async def edit_message_text(self, **kwargs):
                return None

        asyncio.run(download_file_with_progress(
            self.settings,
            str(source.resolve()),
            destination,
            FakeBot(),
            1,
            2,
            source.name,
            source.stat().st_size,
        ))
        self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_persistent_access_mode_and_user_management(self) -> None:
        settings = replace(self.settings, admin_user_ids=frozenset({1}))
        initialize_access_control(self.connection, settings)
        self.assertEqual(access_mode(self.connection), "private")
        self.assertTrue(bot_user_exists(self.connection, 1))
        self.assertTrue(add_bot_user(self.connection, 202020, 1))
        self.assertIn(202020, list_bot_users(self.connection))
        self.assertTrue(remove_bot_user(self.connection, 202020))
        self.assertFalse(bot_user_exists(self.connection, 202020))
        # Startup must not silently restore users that an admin removed.
        initialize_access_control(self.connection, settings)
        self.assertFalse(bot_user_exists(self.connection, 202020))
        private_user = SimpleNamespace(effective_user=SimpleNamespace(id=303030))
        self.assertFalse(authorized(private_user, settings))
        set_access_mode(self.connection, "public")
        self.assertEqual(access_mode(self.connection), "public")
        self.assertTrue(authorized(private_user, settings))
        self.assertFalse(upload_authorized(private_user, settings))
        set_access_mode(self.connection, "private")

    def test_primary_and_dynamic_admin_roles_are_separate_and_persistent(self) -> None:
        settings = replace(
            self.settings,
            admin_user_ids=frozenset({1, 2}),
            primary_admin_user_ids=frozenset({1}),
        )
        self.assertTrue(is_primary_admin(1, settings))
        self.assertFalse(is_primary_admin(2, settings))
        self.assertTrue(is_admin(2, settings))  # Existing admins retain their privileges.
        self.assertFalse(is_admin(303030, settings))
        self.assertTrue(add_bot_admin(self.connection, 303030, 1))
        self.assertTrue(bot_admin_exists(self.connection, 303030))
        self.assertIn(303030, list_bot_admins(self.connection))
        self.assertTrue(is_admin(303030, settings))
        self.assertTrue(remove_bot_admin(self.connection, 303030))
        self.assertFalse(is_admin(303030, settings))

    def test_btree_index_build_reports_sqlite_progress_between_start_and_finish(self) -> None:
        events: list[tuple[str, float]] = []
        for index_name in (
            "idx_records_phone_norm",
            "idx_records_column_1_norm",
            "idx_records_column_2_norm",
            "idx_records_name_norm",
            "idx_records_birth_date_norm",
            "idx_records_record_hash",
        ):
            self.connection.execute(f"DROP INDEX IF EXISTS {index_name}")
        self.connection.execute("UPDATE metadata SET value = '0' WHERE key = 'search_indexes_ready'")
        self.connection.commit()
        with (
            patch("database.INDEX_PROGRESS_HANDLER_INTERVAL_OPS", 1),
            patch("database.INDEX_PROGRESS_MIN_PERCENT_STEP", 0.5),
            patch("database.INDEX_PROGRESS_REPORT_INTERVAL_SECONDS", 3600),
        ):
            build_search_indexes(
                self.connection,
                force=False,
                progress_callback=lambda stage, percent: events.append((stage, percent)),
            )
        phone_progress = [
            percent for stage, percent in events
            if stage == "ساخت شاخص idx_records_phone_norm"
        ]
        self.assertGreater(len(phone_progress), 1)
        self.assertTrue(all(52.0 <= percent < 58.0 for percent in phone_progress))

    def test_selected_columns_only_are_searchable_but_full_row_is_hydrated(self) -> None:
        source = self.data_directory / "selected.csv"
        source.write_text(
            "account_id,name,phone,address\n"
            "786543,سمانه,09123456789,تهران " + "خیابان " * 80 + "آزادی\n",
            encoding="utf-8",
            newline="",
        )
        import_one_file(
            self.connection,
            source,
            self.settings,
            selected_column_indices=(1, 3),
        )
        build_search_indexes(self.connection)

        by_name = search_records(self.connection, "سمانه", 0, self.settings)
        self.assertEqual(by_name.total_count, 1)
        self.assertEqual(by_name.rows[0]["phone"], "09123456789")
        full_fields = parse_raw_fields(by_name.rows[0])
        self.assertEqual(full_fields[:3], ["786543", "سمانه", "09123456789"])
        self.assertTrue(full_fields[3].endswith("آزادی"))
        self.assertFalse(page_needs_full_csv(by_name, self.settings))
        complete_csv = full_results_csv_bytes("سمانه", 0, by_name).decode("utf-8-sig")
        self.assertIn("09123456789", complete_csv)
        self.assertIn(full_fields[3], complete_csv)

        # The account id and phone were not selected, so neither exact nor prefix/FTS
        # search is allowed to find this row through those values.
        self.assertEqual(search_records(self.connection, "786543", 0, self.settings).total_count, 0)
        self.assertEqual(search_records(self.connection, "0912", 0, self.settings).total_count, 0)

        stored = self.connection.execute(
            "SELECT search_text_norm, name_norm, phone_norm FROM records "
            "WHERE source_file_id = (SELECT id FROM imported_files WHERE relative_path = 'selected.csv')"
        ).fetchone()
        self.assertIn("سمانه", stored["search_text_norm"])
        self.assertIn("آزادی", stored["search_text_norm"])
        self.assertNotIn("09123456789", stored["search_text_norm"])
        self.assertEqual(stored["phone_norm"], "")

        # Identical raw rows in distinct uploads must remain independent because their
        # selected search columns can differ per source file.
        second_source = self.data_directory / "selected_phone.csv"
        second_source.write_text(
            "account_id,name,phone,address\n"
            "786543,سمانه,09123456789,تهران خیابان آزادی\n",
            encoding="utf-8",
            newline="",
        )
        import_one_file(
            self.connection,
            second_source,
            self.settings,
            selected_column_indices=(2,),
        )
        by_phone = search_records(self.connection, "09123456789", 0, self.settings)
        self.assertEqual(by_phone.total_count, 1)
        self.assertEqual(by_phone.rows[0]["source_file"], "selected_phone.csv")

    def test_search_requires_the_authoritative_csv_source(self) -> None:
        source = self.data_directory / "sample.csv"
        source.unlink()
        with self.assertRaisesRegex(FileNotFoundError, "required for search hydration"):
            search_records(self.connection, "9359090161", 0, self.settings)

    def test_search_rejects_a_source_changed_after_import(self) -> None:
        source = self.data_directory / "sample.csv"
        source.write_bytes(source.read_bytes() + b"\n")
        with self.assertRaisesRegex(OSError, "changed after import"):
            search_records(self.connection, "9359090161", 0, self.settings)


class LegacySchemaTests(unittest.TestCase):
    def test_legacy_raw_json_database_requires_clear_rebuild(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata VALUES ('schema_version', '1');
                CREATE TABLE records(id INTEGER PRIMARY KEY, raw_fields_json TEXT NOT NULL);
                """
            )
            with self.assertRaisesRegex(DatabaseRebuildRequired, "remove the old search.db"):
                initialize_database(connection)
        finally:
            connection.close()

    def test_schema_v2_database_migrates_selected_columns_without_rebuild(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            initialize_database(connection)
            connection.execute("ALTER TABLE imported_files DROP COLUMN selected_columns_json")
            connection.execute("UPDATE metadata SET value = '2' WHERE key = 'schema_version'")
            connection.commit()
            initialize_database(connection)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(imported_files)")}
            version = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            self.assertIn("selected_columns_json", columns)
            self.assertEqual(version, SCHEMA_VERSION)
        finally:
            connection.close()

    def test_schema_v3_migration_creates_access_and_header_tables(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            initialize_database(connection)
            connection.execute("UPDATE metadata SET value = '3' WHERE key = 'schema_version'")
            connection.commit()
            initialize_database(connection)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertIn("bot_users", tables)
            self.assertIn("bot_admins", tables)
            self.assertIn("header_aliases", tables)
            self.assertIn("hidden_headers", tables)
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION)
        finally:
            connection.close()

    def test_schema_v4_migration_adds_hidden_header_table(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            initialize_database(connection)
            connection.execute("UPDATE metadata SET value = '4' WHERE key = 'schema_version'")
            connection.commit()
            initialize_database(connection)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertIn("hidden_headers", tables)
            self.assertIn("bot_admins", tables)
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION)
        finally:
            connection.close()

    def test_schema_v5_migration_adds_dynamic_admin_table(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            initialize_database(connection)
            connection.execute("UPDATE metadata SET value = '5' WHERE key = 'schema_version'")
            connection.commit()
            initialize_database(connection)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertIn("bot_admins", tables)
            version = connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION)
        finally:
            connection.close()

    def test_new_database_records_current_schema_version(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            initialize_database(connection)
            version = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            self.assertEqual(version, SCHEMA_VERSION)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
