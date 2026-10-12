from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path
import unittest

from config import Settings
from database import (
    DatabaseRebuildRequired,
    SCHEMA_VERSION,
    build_search_indexes,
    connect,
    initialize_database,
)
from import_csv import import_one_file
from search import parse_raw_fields, search_records
from upload_manager import safe_upload_path
from bot import full_results_csv_bytes, page_needs_full_csv


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
        self.assertTrue(page_needs_full_csv(by_name, self.settings))
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
