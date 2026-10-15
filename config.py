"""Configuration loading and validation for the Telegram CSV search service."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import FrozenSet
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent


class ConfigurationError(ValueError):
    """Raised when a required configuration value is missing or invalid."""


def _env_path(value: str, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    if minimum is not None and value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}.")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _parse_user_ids(raw: str, setting_name: str = "ALLOWED_USER_IDS") -> FrozenSet[int]:
    result: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            user_id = int(item)
        except ValueError as exc:
            raise ConfigurationError(f"{setting_name} must contain comma-separated integers.") from exc
        if user_id <= 0:
            raise ConfigurationError(f"{setting_name} must contain positive Telegram user IDs.")
        result.add(user_id)
    return frozenset(result)


def _parse_delimiter(raw: str) -> str:
    if raw == r"\t":
        return "\t"
    if len(raw) != 1:
        raise ConfigurationError("CSV_DELIMITER must be exactly one character (or \\t).")
    return raw


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: FrozenSet[int]
    data_directory: Path
    database_path: Path
    log_directory: Path
    csv_delimiter: str
    csv_encoding: str
    csv_has_header: bool
    csv_column_names: tuple[str, ...]
    duplicate_mode: str
    import_batch_size: int
    import_progress_every_rows: int
    max_results: int
    results_per_page: int
    max_query_length: int
    max_field_display_chars: int
    phone_normalization: bool
    prefix_search_min_length: int
    log_level: str
    upload_enabled: bool
    upload_max_bytes: int
    upload_progress_interval_seconds: float
    delete_csv_after_import: bool
    telegram_base_url: str
    telegram_file_base_url: str
    admin_user_ids: FrozenSet[int] = frozenset()
    initial_access_mode: str = "private"
    primary_admin_user_ids: FrozenSet[int] = frozenset()
    search_audit_enabled: bool = True
    search_audit_store_query: bool = True
    search_audit_retention_days: int = 0
    bot_timezone: str = "Asia/Tehran"
    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        data_directory = _env_path(os.getenv("DATA_DIRECTORY", ""), PROJECT_ROOT / "data")
        database_path = _env_path(os.getenv("DATABASE_PATH", ""), PROJECT_ROOT / "database" / "search.db")
        log_directory = _env_path(os.getenv("LOG_DIRECTORY", ""), PROJECT_ROOT / "logs")
        allowed_user_ids = _parse_user_ids(os.getenv("ALLOWED_USER_IDS", ""))
        configured_admins = os.getenv("ADMIN_USER_IDS", "").strip()
        # Backward compatible safe default: the first bootstrap user is the sole admin.
        if configured_admins:
            configured_admin_ids = _parse_user_ids(configured_admins, "ADMIN_USER_IDS")
        else:
            first_allowed = next((item.strip() for item in os.getenv("ALLOWED_USER_IDS", "").split(",") if item.strip()), "")
            configured_admin_ids = frozenset({int(first_allowed)}) if first_allowed else frozenset()
        configured_primary = os.getenv("PRIMARY_ADMIN_USER_IDS", "").strip()
        if configured_primary:
            primary_admin_user_ids = _parse_user_ids(
                configured_primary, "PRIMARY_ADMIN_USER_IDS"
            )
        else:
            # Existing deployments keep every configured admin. The first one
            # becomes the primary administrator for the new role hierarchy.
            first_admin = next(iter(os.getenv("ADMIN_USER_IDS", "").split(",")), "").strip()
            if not first_admin:
                first_admin = next(
                    (item.strip() for item in os.getenv("ALLOWED_USER_IDS", "").split(",") if item.strip()),
                    "",
                )
            primary_admin_user_ids = frozenset({int(first_admin)}) if first_admin else frozenset()
        admin_user_ids = configured_admin_ids | primary_admin_user_ids
        settings = cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=allowed_user_ids,
            data_directory=data_directory,
            database_path=database_path,
            log_directory=log_directory,
            csv_delimiter=_parse_delimiter(os.getenv("CSV_DELIMITER", ",")),
            csv_encoding=os.getenv("CSV_ENCODING", "utf-8-sig").strip() or "utf-8-sig",
            csv_has_header=_env_bool("CSV_HAS_HEADER", False),
            csv_column_names=tuple(item.strip() for item in os.getenv(
                "CSV_COLUMN_NAMES", "column_1,column_2,name,birth_date,phone"
            ).split(",") if item.strip()),
            # Telegram uploads receive unique temporary names. Exact deduplication
            # prevents re-uploading the same CSV from multiplying the database.
            duplicate_mode=os.getenv("DUPLICATE_MODE", "exact").strip().lower(),
            import_batch_size=_env_int("IMPORT_BATCH_SIZE", 5_000, minimum=100),
            import_progress_every_rows=_env_int("IMPORT_PROGRESS_EVERY_ROWS", 100_000, minimum=1_000),
            max_results=_env_int("MAX_RESULTS", 100, minimum=1),
            results_per_page=_env_int("RESULTS_PER_PAGE", 10, minimum=1),
            max_query_length=_env_int("MAX_QUERY_LENGTH", 160, minimum=1),
            max_field_display_chars=_env_int("MAX_FIELD_DISPLAY_CHARS", 280, minimum=40),
            phone_normalization=_env_bool("PHONE_NORMALIZATION", True),
            prefix_search_min_length=_env_int("PREFIX_SEARCH_MIN_LENGTH", 3, minimum=2),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            upload_enabled=_env_bool("UPLOAD_ENABLED", True),
            # A generous application-side cap; Telegram/account and local Bot API
            # behavior still determines what can actually be transferred.
            upload_max_bytes=_env_int("UPLOAD_MAX_BYTES", 10_737_418_240, minimum=1),
            upload_progress_interval_seconds=float(os.getenv("UPLOAD_PROGRESS_INTERVAL_SECONDS", "3")),
            delete_csv_after_import=_env_bool("DELETE_CSV_AFTER_IMPORT", False),
            # Cloud Bot API is the default. For files over 20 MB, configure a local
            # telegram-bot-api server and set both URLs to its reachable endpoints.
            telegram_base_url=os.getenv("TELEGRAM_BASE_URL", "https://api.telegram.org/bot").rstrip("/"),
            telegram_file_base_url=os.getenv("TELEGRAM_FILE_BASE_URL", "https://api.telegram.org/file/bot").rstrip("/"),
            admin_user_ids=admin_user_ids,
            initial_access_mode=os.getenv("ACCESS_MODE", "private").strip().lower(),
            primary_admin_user_ids=primary_admin_user_ids,
            search_audit_enabled=_env_bool("SEARCH_AUDIT_ENABLED", True),
            search_audit_store_query=_env_bool("SEARCH_AUDIT_STORE_QUERY", True),
            search_audit_retention_days=_env_int("SEARCH_AUDIT_RETENTION_DAYS", 0, minimum=0),
            bot_timezone=os.getenv("BOT_TIMEZONE", "Asia/Tehran").strip() or "Asia/Tehran",
        )
        if settings.results_per_page > settings.max_results:
            raise ConfigurationError("RESULTS_PER_PAGE cannot exceed MAX_RESULTS.")
        if not settings.csv_column_names:
            raise ConfigurationError("CSV_COLUMN_NAMES must contain at least one name.")
        if len(set(settings.csv_column_names)) != len(settings.csv_column_names):
            raise ConfigurationError("CSV_COLUMN_NAMES must not contain duplicates.")
        if settings.duplicate_mode not in {"keep", "exact"}:
            raise ConfigurationError("DUPLICATE_MODE must be either keep or exact.")
        if settings.upload_progress_interval_seconds <= 0:
            raise ConfigurationError("UPLOAD_PROGRESS_INTERVAL_SECONDS must be greater than zero.")
        if settings.delete_csv_after_import:
            raise ConfigurationError(
                "DELETE_CSV_AFTER_IMPORT cannot be enabled: search results are hydrated from the source CSV."
            )
        if not settings.telegram_base_url or not settings.telegram_file_base_url:
            raise ConfigurationError("Telegram API base URLs cannot be empty.")
        if settings.initial_access_mode not in {"private", "public"}:
            raise ConfigurationError("ACCESS_MODE must be private or public.")
        try:
            ZoneInfo(settings.bot_timezone)
        except Exception as exc:
            raise ConfigurationError("BOT_TIMEZONE must be a valid IANA timezone.") from exc
        return settings

    def validate_for_bot(self) -> None:
        if not self.telegram_bot_token:
            raise ConfigurationError("TELEGRAM_BOT_TOKEN is required to run the bot.")
        if not self.admin_user_ids:
            raise ConfigurationError("Set ADMIN_USER_IDS or include at least one initial owner in ALLOWED_USER_IDS.")


def load_settings() -> Settings:
    return Settings.from_env()
