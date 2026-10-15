#!/usr/bin/env python3
"""Authorized Telegram interface for the SQLite CSV search database."""
from __future__ import annotations

import asyncio
import csv
import fcntl
from dataclasses import dataclass, replace
from html import escape
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import tempfile
from threading import Event
import time
import traceback
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from telegram import (
    BotCommand,
    BotCommandScopeChatMember,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TimedOut
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import ConfigurationError, Settings, load_settings
from google_drive import (
    DriveAccessDenied,
    DriveDownloadError,
    DriveFileTooLarge,
    DriveInsufficientSpace,
    DriveNotCsv,
    download_drive_csv,
    parse_drive_share_link,
)
from database import (
    DATABASE_OPERATION_LOCK,
    access_mode as db_access_mode,
    add_bot_admin,
    add_bot_user,
    bot_admin_exists,
    bot_user_exists,
    connect,
    count_completed_files,
    database_statistics,
    delete_imported_file,
    delete_header_alias,
    get_imported_file_details,
    get_header_aliases,
    get_hidden_header_keys,
    header_alias_key,
    hide_header,
    initialize_access_control,
    initialize_database,
    list_completed_files,
    list_bot_users,
    list_bot_admins,
    list_header_alias_entries,
    list_hidden_headers,
    remove_bot_user,
    remove_bot_admin,
    set_access_mode,
    set_header_alias,
    show_header,
)
from logging_utils import configure_logging
from progress_utils import progress_text
from search import SearchPage, parse_raw_fields, search_records
from upload_manager import UploadJob, UploadManager, safe_upload_path
from import_csv import read_csv_header
from search_queue import SearchQueue

LOGGER = logging.getLogger("telegram_csv_search")
SESSION_KEY = "search_sessions"
UPLOAD_SELECTION_KEY = "pending_upload_selection"
UPLOAD_TRANSFER_KEY = "active_upload_transfer"
SEARCH_QUEUE_KEY = "search_queue"
UPLOAD_CANCELLATIONS_KEY = "upload_cancellation_events"
MAX_SESSIONS_PER_USER = 25
UPLOAD_HEADER_PAGE_SIZE = 8
SEARCH_PREVIEW_LIMIT = 5
SEARCH_EXPORT_MAX_RECORDS = 2000
SEARCH_EXPORT_PAGE_SIZE = 1000
SEARCH_EXPORT_CHUNK_BYTES = 40 * 1024 * 1024
DATABASE_MANAGEMENT_PAGE_SIZE = 8


class SearchCancelled(Exception):
    """Raised at safe checkpoints when /start cancels a queued search."""


class UploadCancelled(Exception):
    """Raised by a streaming download when /start requests cancellation."""


def check_search_cancellation(cancellation_event: Event | None) -> None:
    if cancellation_event is not None and cancellation_event.is_set():
        raise SearchCancelled


TEXT_SEND_TIMEOUT_SECONDS = 30


def _log_exception_redacted(message: str, error: BaseException, *secrets_to_redact: str | None) -> None:
    """Log a full traceback while removing credentials and link tokens first."""
    formatted = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    for secret in secrets_to_redact:
        if secret:
            formatted = formatted.replace(secret, "[REDACTED]")
    LOGGER.error("%s\n%s", message, formatted.rstrip())


class BotInstanceAlreadyRunning(RuntimeError):
    """Raised when another bot process already holds this project's lock."""


def acquire_instance_lock(lock_path: Path):
    """Acquire an OS lock so duplicate processes on this host fail before polling."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise BotInstanceAlreadyRunning(
            "یک نمونه از ربات همین پروژه در حال اجراست؛ نمونهٔ دوم اجرا نشد."
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


@dataclass(frozen=True, slots=True)
class QuerySession:
    query: str


@dataclass(slots=True)
class UploadSelectionSession:
    token: str
    job: UploadJob
    headers: tuple[str, ...]
    selected: set[int]
    page: int = 0


def is_primary_admin(user_id: int | None, settings: Settings) -> bool:
    if user_id is None:
        return False
    primary_ids = settings.primary_admin_user_ids
    if primary_ids:
        return user_id in primary_ids
    # Compatibility for Settings objects created by older deployments/tests.
    return bool(settings.admin_user_ids) and user_id == min(settings.admin_user_ids)


def is_admin(user_id: int | None, settings: Settings) -> bool:
    if user_id is None:
        return False
    if user_id in settings.admin_user_ids or is_primary_admin(user_id, settings):
        return True
    try:
        connection = connect(settings.database_path, readonly=True)
        try:
            return bot_admin_exists(connection, user_id)
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        # Fail closed if the role database is unavailable.
        LOGGER.exception("Could not verify dynamic administrator user_id=%s", user_id)
        return False


def access_state(settings: Settings) -> tuple[str, list[int]]:
    connection = connect(settings.database_path, readonly=True)
    try:
        return db_access_mode(connection), list_bot_users(connection)
    finally:
        connection.close()


def authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if is_admin(user.id, settings):
        return True
    connection = connect(settings.database_path, readonly=True)
    try:
        if db_access_mode(connection) == "public":
            return True
        return bot_user_exists(connection, user.id)
    except Exception:
        LOGGER.exception("Could not verify bot access user_id=%s", user.id)
        return False
    finally:
        connection.close()


def upload_authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    if user is None:
        return False
    if is_admin(user.id, settings):
        return True
    connection = connect(settings.database_path, readonly=True)
    try:
        return bot_user_exists(connection, user.id)
    finally:
        connection.close()


def load_display_header_aliases(settings: Settings) -> dict[str, str]:
    connection = connect(settings.database_path, readonly=True)
    try:
        return get_header_aliases(connection)
    finally:
        connection.close()


def load_hidden_header_keys(settings: Settings) -> set[str]:
    connection = connect(settings.database_path, readonly=True)
    try:
        return get_hidden_header_keys(connection)
    finally:
        connection.close()


async def deny_if_unauthorized(update: Update, settings: Settings) -> bool:
    if not update.effective_chat or update.effective_chat.type != "private":
        if update.effective_message:
            await update.effective_message.reply_text(
                "برای حفظ حریم خصوصی، ربات را فقط در گفت‌وگوی خصوصی استفاده کنید."
            )
        return True
    if authorized(update, settings):
        return False
    user_id = update.effective_user.id if update.effective_user else None
    LOGGER.warning("Denied bot access for Telegram user id=%s", user_id)
    if update.effective_message:
        await update.effective_message.reply_text("دسترسی مجاز نیست.")
    return True


async def search_busy_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject extra updates from a user who already has a queued/running search."""
    message = update.effective_message
    if message and message.text:
        first_word = message.text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        if first_word == "/start":
            return
    queue: SearchQueue | None = context.application.bot_data.get(SEARCH_QUEUE_KEY)
    user_id = update.effective_user.id if update.effective_user else None
    user_data = context.user_data or {}
    upload_in_progress = bool(user_data.get(UPLOAD_TRANSFER_KEY))
    selection_pending = bool(user_data.get(UPLOAD_SELECTION_KEY))
    callback = update.callback_query
    if selection_pending and callback and (callback.data or "").startswith("u:"):
        return
    if (
        (queue is None or not queue.is_user_busy(user_id))
        and not upload_in_progress
        and not selection_pending
    ):
        return
    LOGGER.info("operation=request stage=busy_update_rejected user_id=%s", user_id)
    if callback:
        try:
            await callback.answer(
                "در حال پردازش درخواست قبلی شما هستم؛ برای لغو و شروع دوباره /start را بزنید.",
                show_alert=True,
            )
        except Exception:
            LOGGER.exception("operation=search_queue stage=callback_busy_alert_failed user_id=%s", user_id)
    elif message:
        await message.reply_text(
            "در حال پردازش درخواست قبلی شما هستم؛ درخواست جدید ثبت نشد. "
            "برای لغو جست‌وجوهای خودتان و شروع دوباره، /start را بزنید."
        )
    raise ApplicationHandlerStop


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 1)].rstrip() + "…"


def format_record(
    row: Any,
    index: int,
    settings: Settings,
    header_aliases: dict[str, str] | None = None,
    hidden_headers: set[str] | None = None,
) -> str:
    """Render all fields using the source file's real headers."""
    try:
        headers = json.loads(row["headers_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        headers = []
    raw_fields = parse_raw_fields(row)
    if not headers:
        headers = ["column_1", "column_2", "name", "birth_date", "phone"]
    field_count = max(len(headers), len(raw_fields))
    hidden = hidden_headers or set()
    values = [
        (
            (header_aliases or {}).get(
                header_alias_key(str(headers[i]) if i < len(headers) else f"Column {i + 1}"),
                str(headers[i]) if i < len(headers) else f"Column {i + 1}",
            ),
            raw_fields[i] if i < len(raw_fields) else "",
        )
        for i in range(field_count)
        if header_alias_key(str(headers[i]) if i < len(headers) else f"Column {i + 1}") not in hidden
    ]
    lines = ["━━━━━━━━━━━━━━━━━━"]
    for label, value in values:
        # Preserve complete values. A whole message is moved to the TXT fallback
        # when it cannot fit Telegram's message-size limit.
        rendered = value if value else "—"
        lines.append(f"<b>{escape(label)}:</b> {escape(rendered)}")
    lines.append(f"<b>منبع:</b> {escape(str(row['source_file']))}:{row['source_line']}")
    return "\n".join(lines)


def format_full_text_record(
    row: Any,
    index: int,
    header_aliases: dict[str, str] | None = None,
    hidden_headers: set[str] | None = None,
) -> str:
    """Render a complete, untruncated source record as plain UTF-8 text."""
    try:
        headers = json.loads(row["headers_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        headers = []
    fields = parse_raw_fields(row)
    if not headers:
        headers = [f"column_{i + 1}" for i in range(len(fields))]
    aliases = header_aliases or {}
    hidden = hidden_headers or set()
    lines: list[str] = ["━━━━━━━━━━━━━━━━━━"]
    for i in range(max(len(headers), len(fields))):
        original = str(headers[i]) if i < len(headers) else f"column_{i + 1}"
        if header_alias_key(original) in hidden:
            continue
        label = aliases.get(header_alias_key(original), original)
        value = fields[i] if i < len(fields) else ""
        lines.append(f"{label}: {value}" if value else f"{label}: —")
    lines.append(f"منبع: {row['source_file']}:{row['source_line']}")
    return "\n".join(lines) + "\n"


def format_search_preview(
    query: str,
    result: SearchPage,
    settings: Settings,
    header_aliases: dict[str, str] | None = None,
    hidden_headers: set[str] | None = None,
) -> str:
    rows = list(result.rows[:SEARCH_PREVIEW_LIMIT])
    text = "\n".join(
        format_record(row, i + 1, settings, header_aliases, hidden_headers)
        for i, row in enumerate(rows)
    )
    return text


def write_search_text_exports(
    settings: Settings,
    query: str,
    destination: Path,
    header_aliases: dict[str, str] | None = None,
    *,
    page_size: int = SEARCH_EXPORT_PAGE_SIZE,
    max_chunk_bytes: int = SEARCH_EXPORT_CHUNK_BYTES,
    filename_prefix: str = "نتایج_جست‌وجو",
    progress_callback: Callable[[int, int], None] | None = None,
    max_results: int | None = None,
    hidden_headers: set[str] | None = None,
) -> list[Path]:
    """Stream up to max_results matching records to bounded UTF-8 TXT chunks."""
    if page_size < 1 or max_chunk_bytes < 4:
        raise ValueError("page_size must be positive and max_chunk_bytes must be at least 4.")
    export_settings = replace(settings, results_per_page=page_size)
    first = run_search(export_settings, query, 0)
    total = first.total_count
    export_total = min(total, max_results) if max_results is not None else total
    if export_total <= 0:
        return []
    if progress_callback:
        progress_callback(0, export_total)
    destination.mkdir(parents=True, exist_ok=True)
    filename_prefix = re.sub(r"[^\w-]+", "_", filename_prefix, flags=re.UNICODE).strip("_-")
    filename_prefix = truncate(filename_prefix, 64) or "نتایج_جست‌وجو"
    paths: list[Path] = []
    written = 0
    handle = None

    def open_chunk(number: int):
        suffix = "" if number == 1 else f"_{number:03d}"
        path = destination / f"{filename_prefix}{suffix}.txt"
        paths.append(path)
        return path.open("wb")

    def write_payload(payload: bytes) -> None:
        nonlocal handle, written
        while payload:
            if handle is None:
                handle = open_chunk(len(paths) + 1)
                written = 0
            room = max_chunk_bytes - written
            cut = min(len(payload), room)
            if cut < len(payload):
                # Do not split a UTF-8 code point across two files.
                while cut > 0 and (payload[cut] & 0xC0) == 0x80:
                    cut -= 1
                if cut == 0:
                    if written:
                        handle.close()
                        handle = None
                        continue
                    raise ValueError("Could not find a UTF-8 boundary within the configured TXT chunk size.")
            handle.write(payload[:cut])
            payload = payload[cut:]
            written += cut
            if written >= max_chunk_bytes:
                handle.close()
                handle = None
                written = 0

    try:
        for page in range(math.ceil(export_total / page_size)):
            result = first if page == 0 else run_search(
                export_settings, query, page, mode=first.mode, count_total=False
            )
            page_rows = result.rows[: max(0, export_total - page * page_size)]
            for offset, row in enumerate(page_rows):
                record = format_full_text_record(
                    row, page * page_size + offset + 1, header_aliases, hidden_headers
                ).encode("utf-8")
                # Keep ordinary records intact within one part. Only a single row larger
                # than the part limit is split, always at a UTF-8 boundary.
                if handle is not None and written and (
                    written + len(record) > max_chunk_bytes or len(record) > max_chunk_bytes
                ):
                    handle.close()
                    handle = None
                    written = 0
                write_payload(record)
            if progress_callback:
                progress_callback(min((page + 1) * page_size, export_total), export_total)
        if not paths:
            raise RuntimeError("Search count was positive but no rows were returned.")
    finally:
        if handle is not None:
            handle.close()
    return paths



def page_needs_full_csv(
    result: SearchPage,
    settings: Settings,
    hidden_headers: set[str] | None = None,
    header_aliases: dict[str, str] | None = None,
    *,
    query: str = "",
    page: int = 0,
    max_chars: int = 3900,
) -> bool:
    """Return true only when the complete page cannot fit in a Telegram message."""
    complete_page = format_page(
        query,
        page,
        result,
        settings,
        header_aliases=header_aliases,
        hidden_headers=hidden_headers,
        max_chars=None,
    )
    return len(complete_page) > max_chars


def full_results_csv_bytes(
    query: str,
    page: int,
    result: SearchPage,
    header_aliases: dict[str, str] | None = None,
    hidden_headers: set[str] | None = None,
) -> bytes:
    """Serialize visible fields on the current page for complete delivery."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["query", query, "page", page + 1])
    aliases = header_aliases or {}
    hidden = hidden_headers or set()
    for number, row in enumerate(result.rows, start=1):
        try:
            headers = json.loads(row["headers_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            headers = []
        fields = parse_raw_fields(row)
        if not headers:
            headers = [f"column_{index + 1}" for index in range(len(fields))]
        writer.writerow(["result", number, "source", row["source_file"], "line", row["source_line"]])
        visible_indices = [
            index for index in range(max(len(headers), len(fields)))
            if header_alias_key(
                str(headers[index]) if index < len(headers) else f"column_{index + 1}"
            ) not in hidden
        ]
        writer.writerow([
            aliases.get(
                header_alias_key(
                    str(headers[index]) if index < len(headers) else f"column_{index + 1}"
                ),
                str(headers[index]) if index < len(headers) else f"column_{index + 1}",
            )
            for index in visible_indices
        ])
        writer.writerow([fields[index] if index < len(fields) else "" for index in visible_indices])
        writer.writerow([])
    return output.getvalue().encode("utf-8-sig")


def format_page(
    query: str,
    page: int,
    result: SearchPage,
    settings: Settings,
    *,
    full_csv_attached: bool = False,
    header_aliases: dict[str, str] | None = None,
    hidden_headers: set[str] | None = None,
    max_chars: int | None = 3900,
) -> str:
    visible_total = min(result.total_count, settings.max_results)
    start = page * settings.results_per_page + 1
    end = min(start + len(result.rows) - 1, visible_total)
    mode_label = {"exact": "دقیق", "prefix": "ابتدای مقدار", "text": "متنی"}.get(result.mode, "جست‌وجو")
    header = (
        f"<b>جست‌وجو:</b> {escape(truncate(query, settings.max_query_length))}\n"
        f"<b>حالت:</b> {mode_label}\n"
    )
    if full_csv_attached:
        header += "ℹ️ بعضی مقدارهای بلند در پیش‌نمایش کوتاه شده‌اند؛ فایل متنی مقدار کامل را دارد.\n"
    if result.total_count > settings.max_results:
        header += (
            f"<b>تعداد:</b> {result.total_count:,}؛ فقط بخشی از نتیجه‌ها نمایش داده می‌شوند.\n"
        )
    else:
        header += f"<b>تعداد:</b> {result.total_count:,}\n"
    header += f"<b>نمایش:</b> {start:,} تا {end:,}\n"
    records = [
        format_record(row, start + offset, settings, header_aliases, hidden_headers)
        for offset, row in enumerate(result.rows)
    ]
    message = header + "\n".join(records)
    if max_chars is None or len(message) <= max_chars:
        return message
    return message[: max(max_chars - 1, 1)].rsplit("\n", 1)[0] + "\n…"


def pagination_keyboard(token: str, page: int, total_count: int, settings: Settings) -> InlineKeyboardMarkup | None:
    visible_total = min(total_count, settings.max_results)
    if visible_total <= settings.results_per_page:
        return None
    pages = (visible_total + settings.results_per_page - 1) // settings.results_per_page
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(InlineKeyboardButton("← قبلی", callback_data=f"p:{token}:{page - 1}"))
    if page + 1 < pages:
        buttons.append(InlineKeyboardButton("بعدی →", callback_data=f"p:{token}:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


def run_search(
    settings: Settings,
    query: str,
    page: int,
    page_size: int | None = None,
    *,
    mode: str | None = None,
    count_total: bool = True,
) -> SearchPage:
    """Run a query in a worker thread with its own short-lived read-only connection."""
    effective_settings = replace(settings, results_per_page=page_size) if page_size else settings
    # Hydration reads the source CSV after querying SQLite. Keep the lock until
    # hydration is complete so deletion cannot move the source in between.
    with DATABASE_OPERATION_LOCK:
        connection = connect(settings.database_path, readonly=True)
        try:
            return search_records(
                connection, query, page, effective_settings, mode=mode, count_total=count_total
            )
        finally:
            connection.close()


def run_stats(settings: Settings) -> dict[str, object]:
    connection = connect(settings.database_path, readonly=True)
    try:
        return database_statistics(connection, settings.database_path)
    finally:
        connection.close()


def save_session(context: ContextTypes.DEFAULT_TYPE, query: str) -> str:
    sessions: dict[str, QuerySession] = context.user_data.setdefault(SESSION_KEY, {})
    token = secrets.token_urlsafe(8)
    sessions[token] = QuerySession(query=query)
    while len(sessions) > MAX_SESSIONS_PER_USER:
        sessions.pop(next(iter(sessions)))
    return token


async def respond_to_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    raw_query: str,
    *,
    status_message=None,
    cancellation_event: Event | None = None,
) -> None:
    query = raw_query.strip()
    if not query:
        await update.effective_message.reply_text("یک شماره، شناسه، نام یا عبارت برای جست‌وجو بفرستید.")
        return
    if len(query) > settings.max_query_length:
        await update.effective_message.reply_text(
            f"عبارت جست‌وجو بیش از حد طولانی است؛ حداکثر {settings.max_query_length} نویسه مجاز است."
        )
        return
    LOGGER.info(
        "operation=search stage=received user_id=%s query_length=%d",
        update.effective_user.id, len(query),
    )
    status = status_message or await update.effective_message.reply_text(progress_text(
        "🔎 جست‌وجو",
        "در حال آماده‌سازی عبارت و اتصال به پایگاه‌داده",
        0,
    ))
    started = time.perf_counter()
    try:
        check_search_cancellation(cancellation_event)
        await status.edit_text(progress_text(
            "🔎 جست‌وجو",
            "در حال جست‌وجو در شاخص‌های پایگاه‌داده",
            20,
        ))
        result = await asyncio.to_thread(run_search, settings, query, 0, SEARCH_PREVIEW_LIMIT)
        check_search_cancellation(cancellation_event)
        elapsed_ms = (time.perf_counter() - started) * 1000
        LOGGER.info(
            "operation=search stage=query_complete user_id=%s mode=%s results=%d duration_ms=%.1f",
            update.effective_user.id, result.mode, result.total_count, elapsed_ms,
        )
        await status.edit_text(progress_text(
            "🔎 جست‌وجو",
            "خواندن عنوان ستون‌ها و آماده‌سازی نمایش نتیجه",
            60,
            detail=f"تعداد رکوردهای منطبق: {result.total_count:,}",
        ))
        aliases, hidden_headers = await asyncio.gather(
            asyncio.to_thread(load_display_header_aliases, settings),
            asyncio.to_thread(load_hidden_header_keys, settings),
        )
        check_search_cancellation(cancellation_event)
        if not result.total_count:
            LOGGER.info(
                "operation=search stage=complete_no_results user_id=%s duration_ms=%.1f",
                update.effective_user.id, (time.perf_counter() - started) * 1000,
            )
            await status.edit_text(progress_text(
                "✅ جست‌وجو کامل شد",
                "نتیجه‌ای پیدا نشد",
                100,
                detail=f"عبارت جست‌وجو: {escape(query)}\nلطفاً عبارت دیگری را امتحان کنید.",
            ))
            return

        mode_label = {"exact": "دقیق", "prefix": "ابتدای مقدار", "text": "متنی"}.get(
            result.mode, "جست‌وجو"
        )
        preview_requires_txt = False
        if result.total_count <= SEARCH_PREVIEW_LIMIT:
            preview = format_search_preview(query, result, settings, aliases, hidden_headers)
            check_search_cancellation(cancellation_event)
            if len(preview) <= 3900:
                LOGGER.info(
                    "operation=search stage=preview_ready user_id=%s results=%d",
                    update.effective_user.id, result.total_count,
                )
                await status.edit_text(
                    progress_text(
                        "✅ جست‌وجو کامل شد",
                        "نتیجه آماده است",
                        100,
                        detail=(
                            f"عبارت جست‌وجو: {escape(query)}\nنوع جست‌وجو: {mode_label}\n"
                            f"تعداد کل نتیجه‌ها: {result.total_count:,}"
                        ),
                    )
                )
                await update.effective_message.reply_text(
                    preview,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            preview_requires_txt = True
            LOGGER.info(
                "operation=search stage=long_preview_redirected_to_txt user_id=%s results=%d chars=%d",
                update.effective_user.id, result.total_count, len(preview),
            )

        await status.edit_text(progress_text(
            "📄 ساخت فایل نتیجه‌ها",
            f"در حال آماده‌سازی فایل TXT؛ حداکثر {SEARCH_EXPORT_MAX_RECORDS:,} رکورد",
            65,
            detail=f"تعداد کل نتیجه‌ها: {result.total_count:,}",
        ))
        loop = asyncio.get_running_loop()
        export_queue: asyncio.Queue[tuple[int, int] | None] = asyncio.Queue()
        last_export_update = 0.0
        last_export_percent = -1.0

        def export_progress(completed: int, total: int) -> None:
            nonlocal last_export_update, last_export_percent
            check_search_cancellation(cancellation_event)
            now = time.monotonic()
            percent = completed / max(total, 1) * 100
            if (
                completed == 0 or completed >= total or now - last_export_update >= 3
                or percent - last_export_percent >= 5
            ):
                last_export_update = now
                last_export_percent = percent
                LOGGER.info(
                    "operation=search stage=txt_export progress=%.1f records=%d total=%d",
                    percent, completed, total,
                )
                try:
                    loop.call_soon_threadsafe(export_queue.put_nowait, (completed, total))
                except RuntimeError:
                    LOGGER.warning("operation=search stage=txt_progress_loop_unavailable")

        async def consume_export_progress() -> None:
            while True:
                item = await export_queue.get()
                if item is None:
                    return
                completed, total = item
                pct = 65.0 + 22.0 * completed / max(total, 1)
                try:
                    await status.edit_text(progress_text(
                        "📄 ساخت فایل نتیجه‌ها",
                        "در حال نوشتن رکوردها در فایل TXT",
                        pct,
                        detail=f"{completed:,} از {total:,} رکورد پردازش شد",
                    ))
                except Exception:
                    LOGGER.exception("operation=search stage=txt_progress_message_failed")

        progress_task: asyncio.Task | None = asyncio.create_task(
            consume_export_progress(), name="txt-progress"
        )

        async def stop_export_progress() -> None:
            nonlocal progress_task
            if progress_task is None:
                return
            # Let any callbacks queued by the worker thread arrive before the sentinel.
            await asyncio.sleep(0)
            export_queue.put_nowait(None)
            await progress_task
            progress_task = None

        delivery_failure: tuple[str, str] | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="telegram-search-results-") as temp_dir:
                safe_query = re.sub(r"[^\w-]+", "_", query, flags=re.UNICODE).strip("_-")
                safe_query = truncate(safe_query, 48).strip("_-") or "جست‌وجو"
                paths = await asyncio.to_thread(
                    write_search_text_exports,
                    settings,
                    query,
                    Path(temp_dir),
                    aliases,
                    filename_prefix=f"نتایج_{safe_query}",
                    progress_callback=export_progress,
                    max_results=SEARCH_EXPORT_MAX_RECORDS,
                    hidden_headers=hidden_headers,
                )
                check_search_cancellation(cancellation_event)
                await stop_export_progress()
                if not paths:
                    raise RuntimeError("Search returned a positive count but produced no text export files.")
                LOGGER.info(
                    "operation=search stage=txt_export_complete parts=%d results=%d exported=%d",
                    len(paths), result.total_count, min(result.total_count, SEARCH_EXPORT_MAX_RECORDS),
                )
                exported_count = min(result.total_count, SEARCH_EXPORT_MAX_RECORDS)
                notes: list[str] = []
                if result.total_count > SEARCH_EXPORT_MAX_RECORDS:
                    notes.append("فقط ۲۰۰۰ نتیجهٔ اول در فایل آمده است؛ لطفاً دقیق‌تر جست‌وجو کنید.")
                if preview_requires_txt:
                    notes.append("به‌دلیل طول زیاد متن، محتوای کامل رکوردها در این فایل آمده است.")
                cap_note = "\n" + "\n".join(notes) if notes else ""
                await status.edit_text(progress_text(
                    "✅ جست‌وجو کامل شد",
                    "فایل نتیجه آماده و در پیام بعدی ارسال می‌شود",
                    100,
                    detail=(
                        f"عبارت جست‌وجو: {escape(query)}\nنوع جست‌وجو: {mode_label}\n"
                        f"تعداد کل نتیجه‌ها: {result.total_count:,}\n"
                        f"تعداد رکوردهای فایل: {exported_count:,}{cap_note}"
                    ),
                ))
                for part_number, path in enumerate(paths, start=1):
                    check_search_cancellation(cancellation_event)
                    LOGGER.info(
                        "operation=search stage=txt_send_started part=%d parts=%d filename=%s",
                        part_number, len(paths), path.name,
                    )
                    with path.open("rb") as document:
                        await asyncio.wait_for(
                            context.bot.send_document(
                                chat_id=update.effective_chat.id,
                                document=document,
                                filename=path.name,
                                caption=(
                                    f"تعداد کل رکوردهای یافت‌شده: {result.total_count:,}\n"
                                    f"تعداد رکوردهای داخل فایل: {exported_count:,}{cap_note}"
                                ),
                                read_timeout=TEXT_SEND_TIMEOUT_SECONDS,
                                write_timeout=TEXT_SEND_TIMEOUT_SECONDS,
                                connect_timeout=TEXT_SEND_TIMEOUT_SECONDS,
                                pool_timeout=TEXT_SEND_TIMEOUT_SECONDS,
                            ),
                            timeout=TEXT_SEND_TIMEOUT_SECONDS,
                        )
                    LOGGER.info(
                        "operation=search stage=txt_send_complete part=%d parts=%d",
                        part_number, len(paths),
                    )
        except SearchCancelled:
            raise
        except (asyncio.TimeoutError, TimedOut):
            LOGGER.warning(
                "operation=search stage=txt_send_timeout user_id=%s query_length=%d timeout_seconds=%d",
                update.effective_user.id, len(query), TEXT_SEND_TIMEOUT_SECONDS,
            )
            delivery_failure = (
                "❌ ارسال فایل نتیجه کامل نشد",
                "مهلت ارسال ۳۰ ثانیه‌ای به پایان رسید؛ کمی بعد دوباره تلاش کنید.",
            )
        except Exception as exc:
            _log_exception_redacted(
                "operation=search stage=txt_export_or_send_failed user_id=%s error_type=%s"
                % (update.effective_user.id, type(exc).__name__),
                exc,
                getattr(settings, "telegram_bot_token", None),
            )
            delivery_failure = (
                "❌ ساخت یا ارسال فایل نتیجه‌ها ناموفق بود",
                "خطای فنی در گزارش سرور ثبت شده است؛ لطفاً دوباره تلاش کنید.",
            )
        finally:
            await stop_export_progress()
        if delivery_failure is not None:
            title, detail = delivery_failure
            await status.edit_text(progress_text(
                title,
                detail,
                100,
            ))
    except SearchCancelled:
        LOGGER.info("operation=search stage=cancelled_by_start user_id=%s", update.effective_user.id)
        try:
            await status.edit_text(progress_text(
                "⚪ جست‌وجو لغو شد",
                "درخواست جاری با /start لغو شد و نتیجه ارسال نمی‌شود",
                100,
            ))
        except Exception:
            LOGGER.exception("operation=search stage=cancel_status_update_failed user_id=%s", update.effective_user.id)
    except Exception as exc:
        _log_exception_redacted(
            "operation=search stage=search_failed user_id=%s query_length=%d error_type=%s"
            % (update.effective_user.id, len(query), type(exc).__name__),
            exc,
            getattr(settings, "telegram_bot_token", None),
        )
        await status.edit_text(progress_text(
            "❌ جست‌وجو ناموفق بود",
            "خطای فنی در گزارش سرور ثبت شده است؛ لطفاً دوباره تلاش کنید.",
            100,
        ))


async def process_queued_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    *,
    status_message,
    cancellation_event: Event,
) -> None:
    """Adapt SearchQueue's request signature to the search handler's settings argument."""
    settings: Settings = context.application.bot_data["settings"]
    await respond_to_search(
        update,
        context,
        settings,
        query,
        status_message=status_message,
        cancellation_event=cancellation_event,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    user_id = update.effective_user.id
    if is_admin(user_id, settings):
        await _sync_user_command_scope(context.bot, user_id, settings)
    queue: SearchQueue | None = context.application.bot_data.get(SEARCH_QUEUE_KEY)
    cancelled_waiting, active_cancel_requested = (0, False)
    if queue is not None:
        cancelled_waiting, active_cancel_requested = await queue.cancel_user(user_id)
    upload_events: dict[int, Event] = context.application.bot_data.get(UPLOAD_CANCELLATIONS_KEY, {})
    upload_event = upload_events.get(user_id)
    upload_cancel_requested = bool(upload_event and not upload_event.is_set())
    if upload_event:
        upload_event.set()
    context.user_data.pop(SESSION_KEY, None)
    selection = context.user_data.pop(UPLOAD_SELECTION_KEY, None)
    if selection is not None:
        selection.job.path.unlink(missing_ok=True)

    cancellation_note = "جست‌وجوهای منتظر شما لغو شدند." if cancelled_waiting else ""
    if active_cancel_requested:
        cancellation_note += (
            "\nدرخواست جست‌وجوی جاری برای لغو علامت‌گذاری شد و پس از پایان گام فعلی متوقف می‌شود؛ نتیجه‌ای ارسال نخواهد شد."
        )
    if upload_cancel_requested:
        cancellation_note += "\nدانلود جاری Google Drive برای لغو علامت‌گذاری شد؛ فایل ناقص حذف می‌شود."
    if selection is not None:
        cancellation_note += "\nانتخاب ستون فایل ناتمام لغو شد و فایل موقت آن پاک شد."
    if not cancellation_note:
        cancellation_note = "جست‌وجوی جاری یا منتظری برای لغو وجود نداشت."
    lines = [
        "سلام. عبارت موردنظرتان را همین‌جا بفرستید یا از /search <عبارت> استفاده کنید.",
        "ربات در ستون‌هایی می‌گردد که هنگام افزودن فایل برای جست‌وجو انتخاب شده‌اند، اما نتیجه، اطلاعات کامل همان ردیف را نشان می‌دهد.",
        "تا ۵ نتیجه در پیام نمایش داده می‌شود؛ اگر بیشتر باشد، فایل TXT فرستاده می‌شود. خروجی TXT حداکثر ۲۰۰۰ ردیف دارد؛ برای نتیجه‌های بیشتر عبارت را دقیق‌تر کنید.",
        "برای دیدن آمار از /stats استفاده کنید. اگر پردازشی برایتان در حال انجام است، /start درخواست‌های جست‌وجوی شما را لغو می‌کند.",
    ]
    if upload_authorized(update, settings):
        lines.append(
            "برای افزودن داده، فایل CSV/TXT را در گفت‌وگوی خصوصی بفرستید یا لینک اشتراک‌گذاری فایل CSV/TXT در Google Drive را ارسال کنید."
        )
    else:
        lines.append("ارسال فایل برای مدیران و کاربران ثبت‌شده فعال است.")
    if is_admin(user_id, settings):
        lines.append("برای دیدن پنل مدیریت و دسته‌بندی ابزارها از /admin استفاده کنید.")
    lines.extend(["", cancellation_note])
    await update.effective_message.reply_text("\n".join(lines))


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    queue: SearchQueue = context.application.bot_data[SEARCH_QUEUE_KEY]
    await queue.submit(update, context, " ".join(context.args))


def _progress_bar(percent: float, width: int = 20) -> str:
    percent = max(0.0, min(percent, 100.0))
    filled = round(width * percent / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def _human_bytes(value: float) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if number < 1024 or unit == "TiB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{value:.1f} B"


def telegram_file_download_url(settings: Settings, file_path: str) -> str:
    """Build a download URL from Telegram's relative path or absolute URL response."""
    path = file_path.strip()
    if not path:
        raise ValueError("Telegram returned an empty file path.")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Telegram returned an invalid absolute file URL.")
        return path
    # Telegram's URL shape is /file/bot<TOKEN>/<file_path>; there is no slash
    # between the literal "bot" prefix and the token.
    return f"{settings.telegram_file_base_url.rstrip('/')}{settings.telegram_bot_token}/{path.lstrip('/')}"


async def download_file_with_progress(
    settings: Settings,
    file_path: str | None,
    destination: Path,
    bot,
    chat_id: int,
    message_id: int,
    filename: str,
    total_bytes: int | None,
    cancellation_event: Event | None = None,
) -> None:
    """Stream a Telegram file to disk and edit one status message periodically.

    A Local Bot API Server is required for files larger than Telegram's hosted
    Bot API download limit. The configured file base URL is used deliberately so
    both cloud and local Bot API deployments work.
    """
    if not file_path:
        raise RuntimeError("تلگرام مسیر قابل دریافت فایل را برنگرداند.")
    local_source = Path(file_path)
    expected_bytes = total_bytes
    if local_source.is_absolute() and not expected_bytes:
        expected_bytes = local_source.stat().st_size
    started = time.monotonic()
    last_logged_bucket = -1
    last_logged_bytes = 0

    async def update_progress(received: int, *, complete: bool = False) -> None:
        nonlocal last_logged_bucket, last_logged_bytes
        elapsed = max(time.monotonic() - started, 0.001)
        percent = 100.0 if complete else (
            min(100.0, received / expected_bytes * 100) if expected_bytes else 0.0
        )
        if expected_bytes:
            bucket = min(10, int(percent // 10))
            should_log = bucket > last_logged_bucket or complete
            last_logged_bucket = max(last_logged_bucket, bucket)
        else:
            should_log = received - last_logged_bytes >= 64 * 1024 * 1024 or complete
        if should_log:
            LOGGER.info(
                "operation=telegram_download stage=%s progress=%.1f received_bytes=%d expected_bytes=%s speed_bytes_per_second=%.0f",
                "download_complete" if complete else "downloading",
                percent, received, expected_bytes, received / elapsed,
            )
            last_logged_bytes = received
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=progress_text(
                    "⬇️ دریافت فایل از Telegram",
                    "دریافت کامل شد" if complete else "در حال دریافت فایل",
                    percent,
                    detail=(
                        f"حجم دریافت‌شده: {_human_bytes(received)}"
                        + (f" از {_human_bytes(expected_bytes)}" if expected_bytes else "")
                        + f"\nسرعت: {_human_bytes(received / elapsed)} در ثانیه"
                    ),
                ),
            )
        except Exception:
            LOGGER.exception("operation=telegram_download stage=progress_message_update_failed")

    LOGGER.info(
        "operation=telegram_download stage=started filename=%s expected_bytes=%s",
        filename, expected_bytes,
    )
    await update_progress(0)
    if local_source.is_absolute():
        if not local_source.is_file():
            raise FileNotFoundError(
                "Local Bot API returned a filesystem path that is not visible to the bot process; "
                "mount the Bot API data directory into the bot container."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        last_update = 0.0
        with local_source.open("rb") as source, destination.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                if cancellation_event is not None and cancellation_event.is_set():
                    raise UploadCancelled
                if received + len(chunk) > settings.upload_max_bytes:
                    raise ValueError("Telegram file exceeds the configured upload limit.")
                output.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_update >= settings.upload_progress_interval_seconds:
                    await update_progress(received)
                    last_update = now
        await update_progress(received, complete=True)
        if expected_bytes is not None and received != expected_bytes:
            raise ValueError("Telegram file download was incomplete.")
        return
    url = telegram_file_download_url(settings, file_path)
    last_update = 0.0
    received = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    if cancellation_event is not None and cancellation_event.is_set():
                        raise UploadCancelled
                    if received + len(chunk) > settings.upload_max_bytes:
                        raise ValueError("Telegram file exceeds the configured upload limit.")
                    output.write(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if now - last_update >= settings.upload_progress_interval_seconds:
                        await update_progress(received)
                        last_update = now
    await update_progress(received, complete=True)
    if expected_bytes is not None and received != expected_bytes:
        raise ValueError("Telegram file download was incomplete.")


async def text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    queue: SearchQueue = context.application.bot_data[SEARCH_QUEUE_KEY]
    await queue.submit(update, context, update.effective_message.text or "")


def upload_selection_keyboard(session: UploadSelectionSession) -> InlineKeyboardMarkup:
    start = session.page * UPLOAD_HEADER_PAGE_SIZE
    end = min(start + UPLOAD_HEADER_PAGE_SIZE, len(session.headers))
    rows: list[list[InlineKeyboardButton]] = []
    navigation: list[InlineKeyboardButton] = []
    if session.page > 0:
        navigation.append(InlineKeyboardButton("← قبلی", callback_data=f"u:{session.token}:p:{session.page - 1}"))
    if end < len(session.headers):
        navigation.append(InlineKeyboardButton("بعدی →", callback_data=f"u:{session.token}:p:{session.page + 1}"))
    # Put paging controls before the list so they remain visible on mobile clients.
    if navigation:
        rows.append(navigation)
    for index in range(start, end):
        checked = index in session.selected
        label = truncate(session.headers[index], 42)
        rows.append([InlineKeyboardButton(
            f"{'☑' if checked else '☐'} {label}",
            callback_data=f"u:{session.token}:t:{index}",
        )])
    rows.append([
        InlineKeyboardButton("✅ تأیید انتخاب", callback_data=f"u:{session.token}:done"),
        InlineKeyboardButton("لغو", callback_data=f"u:{session.token}:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def upload_selection_prompt(session: UploadSelectionSession) -> str:
    total_pages = max(math.ceil(len(session.headers) / UPLOAD_HEADER_PAGE_SIZE), 1)
    page_note = f"صفحهٔ {session.page + 1} از {total_pages}."
    if total_pages > 1:
        page_note += " برای دیدن بقیهٔ ستون‌ها، «بعدی →» را بالای فهرست بزنید."
    return progress_text(
        "✅ دریافت فایل و خواندن سرستون‌ها کامل شد",
        "در انتظار انتخاب ستون‌ها توسط شما",
        100,
        detail=(
            f"{session.job.original_filename}\n"
            f"ستون‌هایی را که باید در جست‌وجو باشند انتخاب کنید ({len(session.headers)} ستون).\n"
            f"{page_note}\n"
            "برای نمایش نتیجه، تمام ستون‌های همان ردیف از CSV خوانده می‌شوند."
        ),
    )


async def upload_selection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    callback = update.callback_query
    if callback is None:
        return
    if not upload_authorized(update, settings):
        await callback.answer("بارگذاری و مدیریت فایل فقط برای کاربران مجاز است.", show_alert=True)
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await callback.answer("این عملیات فقط در گفت‌وگوی خصوصی ربات مجاز است.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) < 3:
        await callback.answer("این انتخاب معتبر نیست.", show_alert=True)
        return
    sessions = context.user_data
    session = sessions.get(UPLOAD_SELECTION_KEY)
    if not isinstance(session, UploadSelectionSession) or session.token != parts[1]:
        await callback.answer("مهلت انتخاب تمام شده؛ فایل را دوباره بفرستید.", show_alert=True)
        return
    action = parts[2]
    if action == "t" and len(parts) == 4:
        try:
            index = int(parts[3])
        except ValueError:
            await callback.answer("ستون نامعتبر است.", show_alert=True)
            return
        if index < 0 or index >= len(session.headers):
            await callback.answer("ستون نامعتبر است.", show_alert=True)
            return
        if index in session.selected:
            session.selected.remove(index)
        else:
            session.selected.add(index)
        LOGGER.info(
            "operation=upload stage=header_selection_toggled user_id=%s column_index=%d selected_count=%d",
            session.job.uploader_id, index, len(session.selected),
        )
        await callback.answer()
        await callback.edit_message_reply_markup(reply_markup=upload_selection_keyboard(session))
        return
    if action == "p" and len(parts) == 4:
        try:
            page = int(parts[3])
        except ValueError:
            page = -1
        max_page = max((len(session.headers) - 1) // UPLOAD_HEADER_PAGE_SIZE, 0)
        if page < 0 or page > max_page:
            await callback.answer("صفحه نامعتبر است.", show_alert=True)
            return
        session.page = page
        LOGGER.info(
            "operation=upload stage=header_selection_page user_id=%s page=%d total_pages=%d",
            session.job.uploader_id, page + 1, max_page + 1,
        )
        await callback.answer()
        await callback.edit_message_text(
            upload_selection_prompt(session),
            reply_markup=upload_selection_keyboard(session),
        )
        return
    if action == "cancel":
        sessions.pop(UPLOAD_SELECTION_KEY, None)
        session.job.path.unlink(missing_ok=True)
        LOGGER.info(
            "operation=upload stage=cancelled_by_user user_id=%s filename=%s",
            session.job.uploader_id, session.job.original_filename,
        )
        await callback.answer("فایل لغو شد.")
        await callback.edit_message_text(progress_text(
            "⚪ عملیات لغو شد",
            "فایل موقت حذف شد و ورود اطلاعات شروع نشد",
            100,
        ))
        return
    if action == "done":
        if not session.selected:
            await callback.answer("حداقل یک ستون را برای جست‌وجو انتخاب کنید.", show_alert=True)
            return
        job = replace(session.job, selected_column_indices=tuple(sorted(session.selected)))
        manager: UploadManager = context.application.bot_data["upload_manager"]
        position = await manager.enqueue(job)
        LOGGER.info(
            "operation=upload stage=header_selection_confirmed user_id=%s selected_columns=%d queue_position=%d",
            job.uploader_id, len(job.selected_column_indices or ()), position,
        )
        sessions.pop(UPLOAD_SELECTION_KEY, None)
        await callback.answer("انتخاب ثبت شد.")
        await callback.edit_message_text(
            progress_text(
                "📥 ستون‌های انتخابی ثبت شدند",
                f"در صف ورود اطلاعات؛ موقعیت {position}",
                0,
                detail=(
                    f"{job.original_filename}\nجست‌وجو فقط در ستون‌های انتخاب‌شده انجام می‌شود؛ "
                    "همهٔ فیلدهای ردیف نتیجه نمایش داده می‌شوند."
                ),
            )
        )
        return
    await callback.answer("عملیات ناشناخته است.", show_alert=True)


async def show_upload_header_selection(
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    job: UploadJob,
    status_message,
    *,
    cancellation_event: Event | None = None,
) -> None:
    """Read the first CSV row and show the same header picker for every source."""
    LOGGER.info(
        "operation=upload stage=header_read_started user_id=%s filename=%s",
        job.uploader_id, job.original_filename,
    )
    await status_message.edit_text(progress_text(
        "📑 آماده‌سازی فایل برای ورود",
        "در حال خواندن سرستون‌های CSV",
        0,
        detail=job.original_filename,
    ))
    try:
        headers = await asyncio.to_thread(read_csv_header, job.path, settings)
    except Exception:
        LOGGER.exception(
            "operation=upload stage=header_read_failed user_id=%s filename=%s",
            job.uploader_id, job.original_filename,
        )
        job.path.unlink(missing_ok=True)
        await status_message.edit_text(progress_text(
            "❌ خواندن سرستون‌ها ناموفق بود",
            "مرحلهٔ خواندن header",
            100,
            detail="فایل موقت حذف شد و ورود اطلاعات آغاز نشد.",
        ))
        return
    if cancellation_event is not None and cancellation_event.is_set():
        job.path.unlink(missing_ok=True)
        await status_message.edit_text(progress_text(
            "⚪ دریافت فایل لغو شد",
            "درخواست با /start لغو شد؛ فایل موقت پاک شد و ورود اطلاعات شروع نشد",
            100,
        ))
        return
    if context.user_data.get(UPLOAD_SELECTION_KEY):
        job.path.unlink(missing_ok=True)
        await status_message.edit_text(
            "❌ برای این حساب انتخاب ستونی باز است. ابتدا آن را تأیید یا لغو کنید، سپس فایل بعدی را بفرستید."
        )
        return
    selection = UploadSelectionSession(
        token=secrets.token_urlsafe(6),
        job=job,
        headers=tuple(headers),
        selected=set(),
    )
    context.user_data[UPLOAD_SELECTION_KEY] = selection
    LOGGER.info(
        "operation=upload stage=header_read_complete filename=%s columns=%d pages=%d",
        job.original_filename, len(headers), max(math.ceil(len(headers) / UPLOAD_HEADER_PAGE_SIZE), 1),
    )
    await status_message.edit_text(
        upload_selection_prompt(selection),
        reply_markup=upload_selection_keyboard(selection),
    )


async def _download_telegram_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    document,
    filename: str,
    cancellation_event: Event,
) -> None:
    message = update.effective_message
    settings.data_directory.mkdir(parents=True, exist_ok=True)
    temporary_path = safe_upload_path(settings.data_directory, filename)
    final_path = temporary_path.with_suffix("")
    LOGGER.info(
        "operation=telegram_upload stage=upload_received user_id=%s filename=%s bytes=%s",
        update.effective_user.id, filename, document.file_size,
    )
    status_message = await message.reply_text(progress_text(
        "📦 فایل دریافت شد",
        "آماده‌سازی دریافت از Telegram",
        0,
        detail=filename,
    ))
    try:
        LOGGER.info(
            "operation=telegram_upload stage=telegram_get_file_started filename=%s",
            filename,
        )
        telegram_file = await context.bot.get_file(document.file_id)
        await download_file_with_progress(
            settings, telegram_file.file_path, temporary_path, context.bot,
            status_message.chat_id, status_message.message_id, filename, document.file_size,
            cancellation_event,
        )
        actual_size = temporary_path.stat().st_size
        if actual_size > settings.upload_max_bytes:
            temporary_path.unlink(missing_ok=True)
            context.user_data.pop(UPLOAD_TRANSFER_KEY, None)
            events = context.application.bot_data.get(UPLOAD_CANCELLATIONS_KEY, {})
            if events.get(update.effective_user.id) is cancellation_event:
                events.pop(update.effective_user.id, None)
            await status_message.edit_text(
                f"❌ حجم فایل ({actual_size:,} بایت) از سقف تنظیم‌شده ({settings.upload_max_bytes:,} بایت) بیشتر است؛ فایل حذف شد."
            )
            return
        temporary_path.replace(final_path)
        LOGGER.info(
            "operation=telegram_upload stage=download_complete filename=%s bytes=%d path=%s",
            filename, actual_size, final_path,
        )
    except UploadCancelled:
        temporary_path.unlink(missing_ok=True)
        context.user_data.pop(UPLOAD_TRANSFER_KEY, None)
        events = context.application.bot_data.get(UPLOAD_CANCELLATIONS_KEY, {})
        if events.get(update.effective_user.id) is cancellation_event:
            events.pop(update.effective_user.id, None)
        await status_message.edit_text(progress_text(
            "⚪ دریافت فایل لغو شد",
            "درخواست با /start لغو شد؛ فایل موقت پاک شد و ورود اطلاعات شروع نشد",
            100,
        ))
        return
    except Exception as exc:
        detail = str(exc).replace(settings.telegram_bot_token, "[REDACTED]")
        _log_exception_redacted(
            "operation=telegram_upload stage=download_failed user_id=%s filename=%s error_type=%s detail=%s"
            % (update.effective_user.id, filename, type(exc).__name__, detail),
            exc,
            settings.telegram_bot_token,
            document.file_id,
        )
        temporary_path.unlink(missing_ok=True)
        context.user_data.pop(UPLOAD_TRANSFER_KEY, None)
        events = context.application.bot_data.get(UPLOAD_CANCELLATIONS_KEY, {})
        if events.get(update.effective_user.id) is cancellation_event:
            events.pop(update.effective_user.id, None)
        await status_message.edit_text(progress_text(
            "❌ دریافت فایل ناموفق بود",
            "مرحلهٔ دریافت از Telegram",
            100,
            detail="فایل ناقص حذف شد؛ جزئیات و traceback در CMD ثبت شد.",
        ))
        return
    job = UploadJob(
        path=final_path,
        original_filename=filename,
        file_id=document.file_id,
        file_unique_id=document.file_unique_id,
        file_size=final_path.stat().st_size,
        uploader_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        status_message_id=status_message.message_id,
    )
    await show_upload_header_selection(context, settings, job, status_message)
    context.user_data.pop(UPLOAD_TRANSFER_KEY, None)
    events: dict[int, Event] = context.application.bot_data.get(UPLOAD_CANCELLATIONS_KEY, {})
    if events.get(update.effective_user.id) is cancellation_event:
        events.pop(update.effective_user.id, None)


async def document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download an authorized private-chat CSV via the Bot API and queue its import."""
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if not message or not update.effective_chat or update.effective_chat.type != "private":
        return
    if not upload_authorized(update, settings):
        await message.reply_text("بارگذاری فایل فقط برای مدیران و کاربران مجاز است.")
        return
    if not settings.upload_enabled:
        await message.reply_text("بارگذاری فایل با تنظیمات ربات غیرفعال شده است.")
        return
    document = message.document
    if not document:
        return
    filename = document.file_name or "upload.csv"
    if Path(filename).suffix.lower() not in {".csv", ".txt"}:
        await message.reply_text("فقط فایل‌های CSV یا TXT پذیرفته می‌شوند.")
        return
    if document.file_size and document.file_size > settings.upload_max_bytes:
        await message.reply_text(f"حجم فایل از سقف تنظیم‌شده بیشتر است؛ سقف فعلی: {settings.upload_max_bytes:,} بایت است.")
        return
    if context.user_data.get(UPLOAD_TRANSFER_KEY) or context.user_data.get(UPLOAD_SELECTION_KEY):
        await message.reply_text("برای هر حساب هم‌زمان فقط یک فایل را بارگذاری و ستون‌هایش را انتخاب کنید.")
        return
    context.user_data[UPLOAD_TRANSFER_KEY] = "telegram"
    cancellation_event = Event()
    context.application.bot_data.setdefault(UPLOAD_CANCELLATIONS_KEY, {})[
        update.effective_user.id
    ] = cancellation_event
    LOGGER.info(
        "operation=telegram_upload stage=accepted user_id=%s filename=%s expected_bytes=%s",
        update.effective_user.id, filename, document.file_size,
    )
    context.application.create_task(
        _download_telegram_document(
            update, context, settings, document, filename, cancellation_event
        ),
        update=update,
        name="telegram-csv-download",
    )
def _drive_progress_text(
    filename: str,
    received: int,
    total: int | None,
    elapsed: float,
    *,
    complete: bool = False,
) -> str:
    percent = 100.0 if complete else (min(100.0, received / total * 100) if total else 0.0)
    size = f"{_human_bytes(received)} / {_human_bytes(total)}" if total else _human_bytes(received)
    return progress_text(
        "🔗 دریافت فایل از Google Drive",
        "دریافت کامل شد" if complete else "در حال دریافت فایل",
        percent,
        detail=f"{filename}\nحجم: {size}\nسرعت: {_human_bytes(received / max(elapsed, 0.001))} در ثانیه",
    )


async def _download_drive_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: Settings,
    reference,
    temporary_path: Path,
    status_message,
    cancellation_event: Event,
) -> None:
    loop = asyncio.get_running_loop()
    started = time.monotonic()
    last_update = 0.0
    progress_queue: asyncio.Queue[tuple[str, int, int | None] | None] = asyncio.Queue()
    last_logged_bucket = -1
    last_logged_bytes = 0

    async def consume_progress() -> None:
        while True:
            item = await progress_queue.get()
            if item is None:
                return
            filename, received, total = item
            text = _drive_progress_text(
                filename, received, total, time.monotonic() - started
            )
            try:
                await status_message.edit_text(text)
            except Exception:
                LOGGER.debug("Could not refresh Drive download progress user_id=%s", update.effective_user.id)

    progress_task = asyncio.create_task(consume_progress(), name="google-drive-progress-updates")

    def progress(filename: str, received: int, total: int | None) -> None:
        nonlocal last_update, last_logged_bucket, last_logged_bytes
        if cancellation_event.is_set():
            raise UploadCancelled
        now = time.monotonic()
        if received and now - last_update < settings.upload_progress_interval_seconds:
            return
        last_update = now
        percent = min(100.0, received / total * 100) if total else 0.0
        if total:
            bucket = min(10, int(percent // 10))
            should_log = bucket > last_logged_bucket
            last_logged_bucket = max(last_logged_bucket, bucket)
        else:
            should_log = received - last_logged_bytes >= 64 * 1024 * 1024
        if should_log:
            LOGGER.info(
                "operation=drive_upload stage=downloading progress=%.1f received_bytes=%d total_bytes=%s",
                percent, received, total,
            )
            last_logged_bytes = received
        loop.call_soon_threadsafe(progress_queue.put_nowait, (filename, received, total))

    try:
        LOGGER.info(
            "operation=drive_upload stage=download_started user_id=%s",
            update.effective_user.id,
        )
        try:
            info = await asyncio.to_thread(
                download_drive_csv,
                settings,
                reference,
                temporary_path,
                max_bytes=settings.upload_max_bytes,
                progress_callback=progress,
            )
        finally:
            await progress_queue.put(None)
            await progress_task
        if cancellation_event.is_set():
            raise UploadCancelled
        actual_size = temporary_path.stat().st_size
        await status_message.edit_text(_drive_progress_text(
            info.name, actual_size, info.size or actual_size, time.monotonic() - started,
            complete=True,
        ))
        LOGGER.info(
            "operation=drive_upload stage=download_complete filename=%s bytes=%d",
            info.name, actual_size,
        )
        final_path = safe_upload_path(
            settings.data_directory, info.name, source_prefix="drive"
        ).with_suffix("")
        temporary_path.replace(final_path)
        job = UploadJob(
            path=final_path,
            original_filename=info.name,
            file_id=f"drive:{info.file_id}",
            file_unique_id=None,
            file_size=actual_size,
            uploader_id=update.effective_user.id,
            chat_id=update.effective_chat.id,
            status_message_id=status_message.message_id,
            source_kind="google_drive",
        )
        await show_upload_header_selection(
            context, settings, job, status_message, cancellation_event=cancellation_event
        )
    except UploadCancelled:
        temporary_path.unlink(missing_ok=True)
        LOGGER.info(
            "operation=drive_upload stage=cancelled_by_start user_id=%s",
            update.effective_user.id,
        )
        try:
            await status_message.edit_text(progress_text(
                "⚪ دریافت فایل لغو شد",
                "درخواست با /start لغو شد؛ فایل موقت پاک شد و ورود اطلاعات شروع نشد",
                100,
            ))
        except Exception:
            LOGGER.exception("operation=drive_upload stage=cancel_status_update_failed")
    except DriveAccessDenied as exc:
        temporary_path.unlink(missing_ok=True)
        LOGGER.warning(
            "operation=drive_upload stage=access_denied error_type=%s user_id=%s",
            type(exc).__name__, update.effective_user.id,
        )
        await status_message.edit_text(
            progress_text(
                "❌ دسترسی به فایل Drive ممکن نیست",
                "مرحلهٔ بررسی دسترسی فایل",
                100,
                detail=(
                    "در Google Drive، اشتراک‌گذاری را روی «هرکسی که لینک را دارد — مشاهده‌گر» بگذارید. "
                    "فایل خصوصی با روش فعلی قابل دریافت نیست."
                ),
            )
        )
    except DriveFileTooLarge as exc:
        temporary_path.unlink(missing_ok=True)
        await status_message.edit_text(
            progress_text(
                "❌ حجم فایل بیش از سقف مجاز است",
                "مرحلهٔ بررسی اندازهٔ فایل",
                100,
                detail=f"حجم: {_human_bytes(exc.actual)}؛ سقف: {_human_bytes(exc.maximum)}",
            )
        )
    except DriveInsufficientSpace as exc:
        temporary_path.unlink(missing_ok=True)
        await status_message.edit_text(
            progress_text(
                "❌ فضای دیسک کافی نیست",
                "مرحلهٔ بررسی فضای آزاد",
                100,
                detail=f"حجم فایل: {_human_bytes(exc.required)}؛ فضای آزاد: {_human_bytes(exc.available)}",
            )
        )
    except DriveNotCsv as exc:
        temporary_path.unlink(missing_ok=True)
        LOGGER.warning(
            "operation=drive_upload stage=unsupported_file_type error_type=%s",
            type(exc).__name__,
        )
        await status_message.edit_text(progress_text(
            "❌ نوع فایل پشتیبانی نمی‌شود",
            "مرحلهٔ بررسی نوع فایل",
            100,
            detail="این لینک به CSV یا TXT نمی‌رسد؛ Google Sheets/Docs را ابتدا به CSV تبدیل کنید.",
        ))
    except DriveDownloadError as exc:
        temporary_path.unlink(missing_ok=True)
        LOGGER.warning(
            "operation=drive_upload stage=download_failed error_type=%s user_id=%s",
            type(exc).__name__, update.effective_user.id,
        )
        await status_message.edit_text(progress_text(
            "❌ دریافت فایل از Google Drive ناموفق بود",
            "مرحلهٔ دانلود Drive",
            100,
            detail="دسترسی، لینک و اتصال اینترنت را بررسی کنید.",
        ))
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        _log_exception_redacted(
            "operation=drive_upload stage=unexpected_failure user_id=%s error_type=%s"
            % (update.effective_user.id, type(exc).__name__),
            exc,
            reference.file_id,
            reference.resource_key,
        )
        await status_message.edit_text(progress_text(
            "❌ دریافت فایل از Google Drive ناموفق بود",
            "جزئیات و traceback در CMD ثبت شد",
            100,
            detail="فایل ناقص حذف شد و ورود اطلاعات آغاز نشد.",
        ))
    finally:
        context.user_data.pop(UPLOAD_TRANSFER_KEY, None)
        events: dict[int, Event] = context.application.bot_data.get(UPLOAD_CANCELLATIONS_KEY, {})
        if events.get(update.effective_user.id) is cancellation_event:
            events.pop(update.effective_user.id, None)
async def google_drive_link_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept a Google Drive file-sharing link in a private Telegram chat."""
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if not message or not update.effective_chat or update.effective_chat.type != "private":
        return
    if not upload_authorized(update, settings):
        await message.reply_text("دریافت فایل فقط برای مدیران و کاربران مجاز است.")
        return
    if not settings.upload_enabled:
        await message.reply_text("بارگذاری فایل با تنظیمات ربات غیرفعال شده است.")
        return
    if context.user_data.get(UPLOAD_TRANSFER_KEY) or context.user_data.get(UPLOAD_SELECTION_KEY):
        await message.reply_text("برای هر حساب هم‌زمان فقط یک فایل را بارگذاری و ستون‌هایش را انتخاب کنید.")
        return
    try:
        reference = parse_drive_share_link(message.text or "")
    except ValueError:
        await message.reply_text("لینک معتبرِ فایل Google Drive را به‌تنهایی در گفت‌وگوی خصوصی بفرستید؛ لینک پوشه قابل دریافت نیست.")
        return

    settings.data_directory.mkdir(parents=True, exist_ok=True)
    temporary_path = safe_upload_path(settings.data_directory, "drive_upload.csv", source_prefix="drive")
    context.user_data[UPLOAD_TRANSFER_KEY] = "google_drive"
    LOGGER.info(
        "operation=drive_upload stage=link_accepted user_id=%s",
        update.effective_user.id,
    )
    try:
        status_message = await message.reply_text(progress_text(
            "🔎 لینک Drive بررسی شد",
            "آماده‌سازی دانلود",
            0,
        ))
    except Exception:
        context.user_data.pop(UPLOAD_TRANSFER_KEY, None)
        raise
    cancellation_event = Event()
    context.application.bot_data.setdefault(UPLOAD_CANCELLATIONS_KEY, {})[
        update.effective_user.id
    ] = cancellation_event
    context.application.create_task(
        _download_drive_upload(
            update, context, settings, reference, temporary_path, status_message,
            cancellation_event,
        ),
        update=update,
        name="google-drive-csv-download",
    )


async def drive_setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    await update.effective_message.reply_text(
        "برای دریافت فایل از Google Drive نیازی به Google Cloud API یا کلید حساب نیست. "
        "در تنظیمات اشتراک‌گذاری فایل، دسترسی را روی «هرکسی که لینک را دارد — مشاهده‌گر» بگذارید و سپس لینک فایل CSV را در همین گفت‌وگوی خصوصی بفرستید. "
        "لینک پوشه یا Google Sheets پذیرفته نیست؛ فایل باید CSV یا TXT باشد."
    )


def _admin_keyboard(settings: Settings, *, primary_admin: bool = False) -> InlineKeyboardMarkup:
    mode, users = access_state(settings)
    mode_label = "🔓 عمومی‌کردن ربات" if mode == "private" else "🔒 خصوصی‌کردن ربات"
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton("📊 آمار و وضعیت", callback_data="adm:section:status"),
            InlineKeyboardButton("🗄 فایل‌ها و دیتابیس", callback_data="adm:section:data"),
        ],
        [
            InlineKeyboardButton("👥 کاربران و دسترسی", callback_data="adm:section:users"),
            InlineKeyboardButton("🏷 هدرهای خروجی", callback_data="adm:section:headers"),
        ],
        [
            InlineKeyboardButton("📥 فایل‌ها و دریافت", callback_data="adm:section:uploads"),
            InlineKeyboardButton(mode_label, callback_data=f"adm:mode:{'public' if mode == 'private' else 'private'}"),
        ],
    ]
    if primary_admin:
        rows.insert(2, [InlineKeyboardButton("🛡 مدیران ربات", callback_data="adm:section:admins")])
    connection = connect(settings.database_path, readonly=True)
    try:
        dynamic_admins = set(list_bot_admins(connection))
    finally:
        connection.close()
    protected_admins = set(settings.admin_user_ids) | dynamic_admins
    removable = [user_id for user_id in users if user_id not in protected_admins]
    for start in range(0, min(len(removable), 40), 2):
        rows.append([
            InlineKeyboardButton(f"حذف {user_id}", callback_data=f"adm:remove:{user_id}")
            for user_id in removable[start:start + 2]
        ])
    return InlineKeyboardMarkup(rows)


def _admin_panel_text(settings: Settings, *, primary_admin: bool = False) -> str:
    mode, users = access_state(settings)
    mode_text = "عمومی؛ جست‌وجو برای همه باز است" if mode == "public" else "خصوصی؛ فقط کاربران مجاز"
    connection = connect(settings.database_path, readonly=True)
    try:
        dynamic_admins = set(list_bot_admins(connection))
    finally:
        connection.close()
    protected_admins = set(settings.admin_user_ids) | dynamic_admins
    removable_count = sum(user_id not in protected_admins for user_id in users)
    role_note = "مدیر اصلی" if primary_admin else "مدیر"
    return (
        f"پنل مدیریت — {role_note}\n"
        f"وضعیت دسترسی: {mode_text}\n"
        f"کاربران ثبت‌شده: {len(users)} (قابل حذف از دکمه‌ها: {min(removable_count, 40)})\n\n"
        "برای راهنمای هر دسته، یکی از دکمه‌های شیشه‌ای را انتخاب کنید.\n"
        "مدیران فعلی دسترسی خود را حفظ می‌کنند؛ فقط مدیر اصلی می‌تواند نقش مدیران را تغییر دهد.\n"
        "حالت عمومی فقط جست‌وجو را برای همه باز می‌کند؛ بارگذاری فایل همچنان فقط برای کاربران مجاز است."
    )


def _admin_section_view(
    settings: Settings, section: str, *, primary_admin: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    mode, users = access_state(settings)
    mode_text = "عمومی" if mode == "public" else "خصوصی"
    sections = {
        "status": (
            "آمار و وضعیت\nبرای مشاهدهٔ تعداد رکوردها، حجم دیتابیس و تعداد فایل‌های ایندکس‌شده از /stats استفاده کنید."
        ),
        "data": (
            "فایل‌ها و دیتابیس\nاز /database برای دیدن فایل‌های ایندکس‌شده، جزئیات هر فایل و حذف امن یک دیتاست با تأیید استفاده کنید."
        ),
        "users": (
            "کاربران و دسترسی\n"
            "افزودن کاربر: /user_add 123456789\n"
            "حذف کاربر: /user_remove 123456789\n"
            "تغییر حالت دسترسی: /access private یا /access public\n"
            f"حالت فعلی: {mode_text}؛ کاربران ثبت‌شده: {len(users)}."
        ),
        "headers": (
            "هدرهای خروجی\n"
            "تغییر نام: /header_set NATIONAL_CODE = کد ملی\n"
            "فهرست نام‌ها: /header_aliases\n"
            "بازگشت به نام اصلی: /header_remove NATIONAL_CODE\n"
            "مخفی‌کردن: /header_hide NATIONAL_CODE\n"
            "نمایش دوباره: /header_show NATIONAL_CODE\n"
            "فهرست هدرهای مخفی: /header_hidden"
        ),
        "uploads": (
            "بارگذاری و دریافت فایل\n"
            "فایل CSV/TXT را در گفت‌وگوی خصوصی بفرستید یا لینک اشتراک‌گذاری فایل CSV/TXT در Google Drive را ارسال کنید.\n"
            "برای راهنمای اشتراک‌گذاری Drive از /drive_setup استفاده کنید."
        ),
    }
    if section == "admins":
        if not primary_admin:
            return "این بخش فقط برای مدیر اصلی قابل مشاهده است.", InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 بازگشت به پنل مدیریت", callback_data="adm:home:0")]]
            )
        connection = connect(settings.database_path, readonly=True)
        try:
            dynamic_admins = list_bot_admins(connection)
        finally:
            connection.close()
        all_admins = sorted(set(settings.admin_user_ids) | set(dynamic_admins))
        admin_lines = "\n".join(f"• {user_id}" for user_id in all_admins) or "• هنوز مدیری ثبت نشده است"
        text = (
            "مدیران ربات\n"
            f"مدیر اصلی: {', '.join(map(str, sorted(settings.primary_admin_user_ids))) or 'اولین مدیر پیکربندی‌شده'}\n"
            f"مدیران فعلی:\n{admin_lines}\n\n"
            "افزودن مدیر: /admin_add 123456789\n"
            "حذف نقش مدیر: /admin_remove 123456789\n"
            "مدیران تعریف‌شده در فایل .env فقط با تغییر همان فایل قابل حذف‌اند."
        )
    else:
        text = sections.get(section, "این بخش شناخته نشد.")
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 بازگشت به پنل مدیریت", callback_data="adm:home:0")]]
    )
    return text, keyboard


async def _deny_non_admin(update: Update, settings: Settings) -> bool:
    if not is_admin(update.effective_user.id if update.effective_user else None, settings):
        if update.effective_message:
            await update.effective_message.reply_text("این دستور فقط برای مدیر ربات مجاز است.")
        return True
    if not update.effective_chat or update.effective_chat.type != "private":
        if update.effective_message:
            await update.effective_message.reply_text("فرمان‌های مدیریت را فقط در گفت‌وگوی خصوصی با ربات اجرا کنید.")
        return True
    return False


async def _deny_non_primary_admin(update: Update, settings: Settings) -> bool:
    if await _deny_non_admin(update, settings):
        return True
    if not is_primary_admin(update.effective_user.id, settings):
        if update.effective_message:
            await update.effective_message.reply_text("تغییر نقش مدیران فقط در اختیار مدیر اصلی ربات است.")
        return True
    return False


def _user_bot_commands() -> list[BotCommand]:
    return [
        BotCommand("start", "راهنمای استفاده و لغو درخواست جاری"),
        BotCommand("search", "جست‌وجو با عبارت دلخواه"),
        BotCommand("stats", "آمار پایگاه‌داده"),
    ]


def _admin_bot_commands(*, primary_admin: bool) -> list[BotCommand]:
    commands = _user_bot_commands() + [
        BotCommand("admin", "پنل مدیریت دسته‌بندی‌شده"),
        BotCommand("database", "مدیریت فایل‌های ایندکس‌شده"),
        BotCommand("user_add", "افزودن کاربر مجاز"),
        BotCommand("user_remove", "قطع دسترسی یک کاربر"),
        BotCommand("access", "تغییر حالت خصوصی/عمومی"),
        BotCommand("header_set", "تغییر نام نمایشی هدر"),
        BotCommand("header_hide", "مخفی‌کردن هدر در خروجی"),
        BotCommand("header_show", "نمایش دوبارهٔ هدر"),
        BotCommand("header_aliases", "فهرست نام‌های نمایشی هدر"),
        BotCommand("header_hidden", "فهرست هدرهای مخفی"),
        BotCommand("header_remove", "بازگرداندن نام اصلی هدر"),
        BotCommand("drive_setup", "راهنمای دریافت فایل از Drive"),
    ]
    if primary_admin:
        commands.extend([
            BotCommand("admin_add", "افزودن مدیر جدید"),
            BotCommand("admin_remove", "حذف نقش مدیر"),
        ])
    return commands


async def _sync_user_command_scope(bot, user_id: int, settings: Settings, *, remove_admin: bool = False) -> None:
    scope = BotCommandScopeChatMember(chat_id=int(user_id), user_id=int(user_id))
    try:
        if remove_admin:
            await bot.delete_my_commands(scope=scope)
        else:
            await bot.set_my_commands(
                _admin_bot_commands(primary_admin=is_primary_admin(user_id, settings)),
                scope=scope,
            )
    except Exception:
        LOGGER.exception("Could not synchronize Telegram command menu user_id=%s", user_id)


async def _sync_bot_command_menus(bot, settings: Settings) -> None:
    """Publish ordinary commands by default and elevated commands only per admin."""
    try:
        await bot.set_my_commands(_user_bot_commands(), scope=BotCommandScopeDefault())
    except Exception:
        LOGGER.exception("Could not set the default Telegram command menu")
    connection = connect(settings.database_path, readonly=True)
    try:
        dynamic_admins = set(list_bot_admins(connection))
    finally:
        connection.close()
    admin_ids = set(settings.admin_user_ids) | set(settings.primary_admin_user_ids) | dynamic_admins
    for user_id in sorted(admin_ids):
        await _sync_user_command_scope(bot, user_id, settings)


async def admin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_primary_admin(update, settings):
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        await update.effective_message.reply_text("روش درست: /admin_add 123456789")
        return
    user_id = int(context.args[0])
    if is_admin(user_id, settings):
        await update.effective_message.reply_text(f"شناسهٔ {user_id} از قبل مدیر است.")
        return
    connection = connect(settings.database_path)
    try:
        added = add_bot_admin(connection, user_id, update.effective_user.id)
    finally:
        connection.close()
    if added:
        await _sync_user_command_scope(context.bot, user_id, settings)
    await update.effective_message.reply_text(
        f"شناسهٔ {user_id} به مدیران اضافه شد و دسترسی مدیریتی گرفت."
        if added else f"شناسهٔ {user_id} از قبل مدیر است."
    )


async def admin_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_primary_admin(update, settings):
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        await update.effective_message.reply_text("روش درست: /admin_remove 123456789")
        return
    user_id = int(context.args[0])
    if is_primary_admin(user_id, settings):
        await update.effective_message.reply_text("نقش مدیر اصلی از داخل ربات قابل حذف نیست.")
        return
    if user_id in settings.admin_user_ids:
        await update.effective_message.reply_text(
            "این مدیر در ADMIN_USER_IDS فایل .env تعریف شده است؛ برای حذفش همان تنظیم را ویرایش و ربات را restart کنید."
        )
        return
    connection = connect(settings.database_path)
    try:
        removed = remove_bot_admin(connection, user_id)
    finally:
        connection.close()
    if removed:
        # The admin-specific menu is deleted; the ordinary menu becomes visible.
        await _sync_user_command_scope(context.bot, user_id, settings, remove_admin=True)
    await update.effective_message.reply_text(
        f"نقش مدیریتی شناسهٔ {user_id} حذف شد؛ اگر کاربر ثبت‌شده باشد، دسترسی عادی‌اش باقی می‌ماند."
        if removed else f"شناسهٔ {user_id} مدیر پویای ثبت‌شده نبود."
    )


def _database_file_display_name(row: Any) -> str:
    original = row["telegram_filename"]
    if original:
        return str(original)
    name = Path(str(row["relative_path"])).name
    if name.startswith((".telegram-", ".drive-")):
        parts = name.split("-", 2)
        if len(parts) == 3:
            name = parts[2]
    return name


def _database_list_view(settings: Settings, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
    connection = connect(settings.database_path, readonly=True)
    try:
        total = count_completed_files(connection)
        total_pages = max(math.ceil(total / DATABASE_MANAGEMENT_PAGE_SIZE), 1)
        page = min(max(int(page), 0), total_pages - 1)
        files = list_completed_files(
            connection,
            limit=DATABASE_MANAGEMENT_PAGE_SIZE,
            offset=page * DATABASE_MANAGEMENT_PAGE_SIZE,
        )
    finally:
        connection.close()

    rows: list[list[InlineKeyboardButton]] = []
    if total_pages > 1:
        navigation: list[InlineKeyboardButton] = []
        if page > 0:
            navigation.append(InlineKeyboardButton("← قبلی", callback_data=f"db:list:{page - 1}"))
        if page + 1 < total_pages:
            navigation.append(InlineKeyboardButton("بعدی →", callback_data=f"db:list:{page + 1}"))
        rows.append(navigation)
    for item in files:
        label = truncate(_database_file_display_name(item), 38)
        rows.append([InlineKeyboardButton(
            f"📄 {label} · {int(item['record_count']):,}",
            callback_data=f"db:file:{int(item['id'])}:{page}",
        )])

    text = (
        "مدیریت فایل‌های پایگاه‌داده\n"
        f"فایل‌های ایندکس‌شده: {total}\n"
        f"صفحهٔ {page + 1} از {total_pages}\n"
        "برای دیدن جزئیات یا حذف، روی نام فایل بزنید. حذف فقط پس از تأیید نهایی انجام می‌شود."
    )
    if total == 0:
        text += "\nهنوز فایلی با وضعیت کامل در پایگاه‌داده وجود ندارد."
    return text, InlineKeyboardMarkup(rows) if rows else None


def _database_file_view(
    settings: Settings, file_id: int, page: int
) -> tuple[str, InlineKeyboardMarkup] | None:
    connection = connect(settings.database_path, readonly=True)
    try:
        row = get_imported_file_details(connection, file_id)
        details = dict(row) if row else None
    finally:
        connection.close()
    if details is None:
        return None
    name = _database_file_display_name(details)
    text = (
        f"📄 فایل: {name}\n"
        f"ردیف‌های ایندکس‌شده: {int(details['record_count']):,}\n"
        f"حجم CSV خام: {_human_bytes(int(details['file_size']))}\n"
        f"مسیر ثبت‌شده: {details['relative_path']}\n\n"
        "برای خارج‌کردن این فایل از نتایج جست‌وجو، دکمهٔ حذف را بزنید."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 حذف از پایگاه‌داده", callback_data=f"db:confirm:{file_id}:{page}")],
        [InlineKeyboardButton("↩️ بازگشت به فهرست", callback_data=f"db:list:{page}")],
    ])
    return text, keyboard


def _archive_and_delete_database_file_unlocked(
    settings: Settings,
    file_id: int,
    progress_callback: Callable[[str, float], None] | None = None,
):
    """Remove the index and move its source CSV outside DATA_DIRECTORY, not delete it."""
    def report(stage: str, percent: float) -> None:
        if progress_callback:
            try:
                progress_callback(stage, percent)
            except Exception:
                LOGGER.exception("operation=database_delete stage=progress_callback_failed file_id=%s", file_id)

    report("در حال بازکردن پایگاه‌داده", 0.0)
    connection = connect(settings.database_path)
    source_path: Path | None = None
    archived_path: Path | None = None
    try:
        row = get_imported_file_details(connection, file_id)
        if row is None:
            report("فایل در پایگاه‌داده پیدا نشد", 100.0)
            return None
        report("بررسی وضعیت و مسیر فایل", 15.0)
        if str(row["status"]) != "completed":
            raise ValueError("Only completed imports can be removed from the database.")

        data_root = settings.data_directory.resolve()
        relative_path = Path(str(row["relative_path"]))
        candidate = data_root / relative_path
        if candidate.is_symlink():
            raise ValueError("Refusing to archive a symbolic-link source file.")
        source_path = candidate.resolve()
        try:
            source_path.relative_to(data_root)
        except ValueError as exc:
            raise ValueError("The source path is outside the configured data directory.") from exc

        if source_path.is_file():
            archive_root = data_root.parent / f"{data_root.name}_deleted_sources"
            if archive_root.resolve().is_relative_to(data_root):
                raise ValueError("The source archive must be outside the configured data directory.")
            archived_path = archive_root / f"file_{int(file_id)}" / source_path.name
            if archived_path.exists():
                archived_path = archived_path.with_name(
                    f"{archived_path.stem}_{secrets.token_hex(4)}{archived_path.suffix}"
                )
            archived_path.parent.mkdir(parents=True, exist_ok=True)
            report("انتقال CSV خام به آرشیو امن", 40.0)
            shutil.move(str(source_path), str(archived_path))

        try:
            report("حذف ردیف‌ها و شاخص‌های این فایل از SQLite", 65.0)
            result = delete_imported_file(connection, file_id)
            if result is None:
                raise RuntimeError("The imported file disappeared before deletion completed.")
        except BaseException:
            if archived_path and archived_path.exists() and source_path and not source_path.exists():
                source_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(archived_path), str(source_path))
            raise
        report("حذف دیتاست کامل شد", 100.0)
        return result, archived_path
    finally:
        connection.close()


def _archive_and_delete_database_file(
    settings: Settings,
    file_id: int,
    progress_callback: Callable[[str, float], None] | None = None,
):
    """Move a source and remove its index without racing search/import work."""
    with DATABASE_OPERATION_LOCK:
        return _archive_and_delete_database_file_unlocked(
            settings, file_id, progress_callback
        )


async def database_management_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    text, keyboard = _database_list_view(settings, 0)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def database_management_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    callback = update.callback_query
    if callback is None:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await callback.answer("این پنل فقط در گفت‌وگوی خصوصی کار می‌کند.", show_alert=True)
        return
    if not is_admin(update.effective_user.id if update.effective_user else None, settings):
        await callback.answer("فقط مدیر مجاز است.", show_alert=True)
        return

    parts = (callback.data or "").split(":")
    try:
        if len(parts) == 3 and parts[1] == "list":
            text, keyboard = _database_list_view(settings, int(parts[2]))
            await callback.answer()
            await callback.edit_message_text(text, reply_markup=keyboard)
            return

        if len(parts) == 4 and parts[1] in {"file", "confirm", "delete"}:
            file_id, page = int(parts[2]), max(0, int(parts[3]))
        else:
            await callback.answer("درخواست نامعتبر است.", show_alert=True)
            return
    except ValueError:
        await callback.answer("شناسهٔ فایل یا صفحه نامعتبر است.", show_alert=True)
        return

    action = parts[1]
    if action == "file":
        view = _database_file_view(settings, file_id, page)
        if view is None:
            await callback.answer("این فایل دیگر در پایگاه‌داده نیست.", show_alert=True)
            text, keyboard = _database_list_view(settings, page)
            await callback.edit_message_text(text, reply_markup=keyboard)
            return
        await callback.answer()
        await callback.edit_message_text(view[0], reply_markup=view[1])
        return

    if action == "confirm":
        view = _database_file_view(settings, file_id, page)
        if view is None:
            await callback.answer("این فایل دیگر در پایگاه‌داده نیست.", show_alert=True)
            return
        connection = connect(settings.database_path, readonly=True)
        try:
            row = get_imported_file_details(connection, file_id)
            name = _database_file_display_name(row) if row else ""
            record_count = int(row["record_count"]) if row else 0
        finally:
            connection.close()
        archive_name = f"{settings.data_directory.name}_deleted_sources"
        confirm_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأیید حذف از پایگاه‌داده", callback_data=f"db:delete:{file_id}:{page}")],
            [InlineKeyboardButton("لغو", callback_data=f"db:file:{file_id}:{page}")],
        ])
        text = (
            f"⚠️ تأیید حذف فایل\n{name}\n"
            f"{record_count:,} ردیف از نتایج و پایگاه‌داده حذف می‌شود.\n"
            f"CSV خام پاک نمی‌شود؛ به پوشهٔ {archive_name} بیرون از پوشهٔ داده منتقل می‌شود "
            "تا بعداً ناخواسته دوباره ایندکس نشود.\n\nآیا ادامه می‌دهید؟"
        )
        await callback.answer()
        await callback.edit_message_text(text, reply_markup=confirm_markup)
        return

    if action == "delete":
        LOGGER.info(
            "operation=database_delete stage=started user_id=%s file_id=%s",
            update.effective_user.id, file_id,
        )
        await callback.answer("حذف دیتاست آغاز شد.")
        await callback.edit_message_text(progress_text(
            "🗑️ حذف دیتاست",
            "آماده‌سازی حذف و آرشیو فایل خام",
            0,
        ))
        loop = asyncio.get_running_loop()
        progress_queue: asyncio.Queue[tuple[str, float] | None] = asyncio.Queue()

        def delete_progress(stage: str, percent: float) -> None:
            stage_code = {
                "در حال بازکردن پایگاه‌داده": "open_database",
                "فایل در پایگاه‌داده پیدا نشد": "file_not_found",
                "بررسی وضعیت و مسیر فایل": "validate_source",
                "انتقال CSV خام به آرشیو امن": "archive_source",
                "حذف ردیف‌ها و شاخص‌های این فایل از SQLite": "delete_records",
                "حذف دیتاست کامل شد": "delete_complete",
            }.get(stage, "delete_progress")
            LOGGER.info(
                "operation=database_delete stage=%s progress=%.1f file_id=%s",
                stage_code, percent, file_id,
            )
            loop.call_soon_threadsafe(progress_queue.put_nowait, (stage, percent))

        async def consume_delete_progress() -> None:
            while True:
                item = await progress_queue.get()
                if item is None:
                    return
                stage, percent = item
                try:
                    await callback.edit_message_text(progress_text(
                        "🗑️ حذف دیتاست",
                        stage,
                        percent,
                    ))
                except Exception:
                    LOGGER.exception("operation=database_delete stage=progress_message_failed")

        progress_task = asyncio.create_task(consume_delete_progress(), name="delete-progress")
        delete_error: Exception | None = None
        result = None
        try:
            result = await asyncio.to_thread(
                _archive_and_delete_database_file, settings, file_id, delete_progress
            )
        except Exception as exc:
            delete_error = exc
        finally:
            loop.call_soon_threadsafe(progress_queue.put_nowait, None)
            await progress_task
        if delete_error is not None:
            _log_exception_redacted(
                "operation=database_delete stage=failed file_id=%s error_type=%s"
                % (file_id, type(delete_error).__name__),
                delete_error,
                settings.telegram_bot_token,
            )
            await callback.edit_message_text(
                progress_text(
                    "❌ حذف انجام نشد",
                    "اطلاعات دیتابیس بررسی و در صورت نیاز CSV بازگردانده شد",
                    100,
                    detail="جزئیات فنی در CMD ثبت شده است.",
                ),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("بازگشت به فهرست", callback_data=f"db:list:{page}")
                ]]),
            )
            return
        if result is None:
            await callback.edit_message_text(progress_text(
                "⚪ فایل دیگر در پایگاه‌داده نیست",
                "عملیات پایان یافت",
                100,
            ))
            return
        deleted, archived_path = result
        LOGGER.info(
            "Deleted imported dataset file_id=%s rows=%s source_path=%s archive_path=%s",
            file_id, deleted["record_count"], deleted["relative_path"], archived_path,
        )
        notice = (
            f"✅ فایل از پایگاه‌داده حذف شد؛ {int(deleted['record_count']):,} ردیف دیگر در جست‌وجو نمی‌آید."
        )
        if archived_path:
            notice += f"\nفایل خام در آرشیو نگه‌داری شد: {archived_path}"
        else:
            notice += "\nفایل خام پیدا نشد؛ فقط اطلاعات پایگاه‌داده حذف شد."
        text, keyboard = _database_list_view(settings, page)
        LOGGER.info(
            "operation=database_delete stage=complete file_id=%s deleted_rows=%s",
            file_id, deleted["record_count"],
        )
        await callback.edit_message_text(
            progress_text(
                "✅ حذف دیتاست کامل شد",
                "فایل خام در آرشیو نگه‌داری شد و از نتایج حذف شد",
                100,
            ) + f"\n\n{notice}\n\n{text}",
            reply_markup=keyboard,
        )


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    primary_admin = is_primary_admin(update.effective_user.id, settings)
    await update.effective_message.reply_text(
        _admin_panel_text(settings, primary_admin=primary_admin),
        reply_markup=_admin_keyboard(settings, primary_admin=primary_admin),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    callback = update.callback_query
    if callback is None:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await callback.answer("این پنل فقط در گفت‌وگوی خصوصی ربات کار می‌کند.", show_alert=True)
        return
    user_id = update.effective_user.id if update.effective_user else None
    if not is_admin(user_id, settings):
        await callback.answer("فقط مدیر مجاز است.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    action, value = parts[1], parts[2]
    primary_admin = is_primary_admin(user_id, settings)
    if action == "home":
        await callback.answer()
        await callback.edit_message_text(
            _admin_panel_text(settings, primary_admin=primary_admin),
            reply_markup=_admin_keyboard(settings, primary_admin=primary_admin),
        )
        return
    if action == "section":
        await callback.answer()
        text, keyboard = _admin_section_view(settings, value, primary_admin=primary_admin)
        await callback.edit_message_text(text, reply_markup=keyboard)
        return
    if action == "mode" and value in {"private", "public"}:
        connection = connect(settings.database_path)
        try:
            set_access_mode(connection, value)
        finally:
            connection.close()
        await callback.answer("حالت دسترسی تغییر کرد.")
    elif action == "remove":
        try:
            user_id = int(value)
        except ValueError:
            await callback.answer("شناسه نامعتبر است.", show_alert=True)
            return
        if is_admin(user_id, settings):
            await callback.answer("دسترسی مدیر از این پنل حذف نمی‌شود.", show_alert=True)
            return
        connection = connect(settings.database_path)
        try:
            removed = remove_bot_user(connection, user_id)
        finally:
            connection.close()
        await callback.answer("کاربر حذف شد." if removed else "این کاربر در فهرست نبود.")
    else:
        await callback.answer("عملیات ناشناخته است.", show_alert=True)
        return
    await callback.edit_message_text(
        _admin_panel_text(settings, primary_admin=primary_admin),
        reply_markup=_admin_keyboard(settings, primary_admin=primary_admin),
    )


async def user_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        await update.effective_message.reply_text("روش درست: /user_add 123456789")
        return
    user_id = int(context.args[0])
    connection = connect(settings.database_path)
    try:
        added = add_bot_user(connection, user_id, update.effective_user.id)
    finally:
        connection.close()
    await update.effective_message.reply_text(
        f"شناسه {user_id} به کاربران مجاز اضافه شد." if added else f"شناسه {user_id} از قبل مجاز است."
    )


async def user_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    if len(context.args) != 1 or not context.args[0].isdigit() or int(context.args[0]) <= 0:
        await update.effective_message.reply_text("روش درست: /user_remove 123456789")
        return
    user_id = int(context.args[0])
    if is_admin(user_id, settings):
        await update.effective_message.reply_text("شناسه‌ی مدیر اصلی از این فهرست حذف نمی‌شود.")
        return
    connection = connect(settings.database_path)
    try:
        removed = remove_bot_user(connection, user_id)
    finally:
        connection.close()
    await update.effective_message.reply_text(
        f"دسترسی شناسه {user_id} قطع شد." if removed else f"شناسه {user_id} در فهرست کاربران نبود."
    )


async def access_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    if len(context.args) != 1 or context.args[0].lower() not in {"private", "public"}:
        await update.effective_message.reply_text("روش درست: /access private یا /access public")
        return
    mode = context.args[0].lower()
    connection = connect(settings.database_path)
    try:
        set_access_mode(connection, mode)
    finally:
        connection.close()
    response = (
        "ربات اکنون عمومی است؛ همه می‌توانند جست‌وجو کنند، اما بارگذاری فقط برای کاربران مجاز است."
        if mode == "public"
        else "ربات اکنون خصوصی است؛ فقط مدیران و کاربران ثبت‌شده می‌توانند استفاده کنند."
    )
    await update.effective_message.reply_text(response)


async def header_set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    text = update.effective_message.text or ""
    _, _, payload = text.partition(" ")
    source, separator, display = payload.partition("=")
    if not separator or not source.strip() or not display.strip():
        await update.effective_message.reply_text("روش درست: /header_set NATIONAL_CODE = کد ملی")
        return
    connection = connect(settings.database_path)
    try:
        set_header_alias(connection, source.strip(), display.strip())
    finally:
        connection.close()
    await update.effective_message.reply_text(
        f"نام نمایشیِ «{source.strip()}» برای همه‌ی فایل‌ها به «{display.strip()}» تغییر کرد."
    )


async def header_aliases_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    connection = connect(settings.database_path, readonly=True)
    try:
        entries = list_header_alias_entries(connection)
    finally:
        connection.close()
    if not entries:
        await update.effective_message.reply_text("هنوز نام نمایشی سفارشی برای هیچ هدر ثبت نشده است.")
        return
    lines = ["نام‌های نمایشی هدرها:"]
    lines.extend(f"{source} → {display}" for source, display in entries)
    await update.effective_message.reply_text("\n".join(lines))


async def header_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    text = update.effective_message.text or ""
    _, _, source = text.partition(" ")
    if not source.strip():
        await update.effective_message.reply_text("روش درست: /header_remove NATIONAL_CODE")
        return
    connection = connect(settings.database_path)
    try:
        removed = delete_header_alias(connection, source.strip())
    finally:
        connection.close()
    await update.effective_message.reply_text(
        f"نام اصلی هدر «{source.strip()}» دوباره نمایش داده می‌شود."
        if removed else "برای این هدر نام نمایشی سفارشی ثبت نشده بود."
    )


async def header_hide_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    text = update.effective_message.text or ""
    _, _, source = text.partition(" ")
    if not source.strip():
        await update.effective_message.reply_text("روش درست: /header_hide NATIONAL_CODE")
        return
    connection = connect(settings.database_path)
    try:
        changed = hide_header(connection, source.strip())
    finally:
        connection.close()
    await update.effective_message.reply_text(
        f"هدر «{source.strip()}» از این پس در پیام نتایج و فایل TXT نمایش داده نمی‌شود."
        if changed else f"هدر «{source.strip()}» از قبل مخفی بوده است."
    )


async def header_show_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    text = update.effective_message.text or ""
    _, _, source = text.partition(" ")
    if not source.strip():
        await update.effective_message.reply_text("روش درست: /header_show NATIONAL_CODE")
        return
    connection = connect(settings.database_path)
    try:
        changed = show_header(connection, source.strip())
    finally:
        connection.close()
    await update.effective_message.reply_text(
        f"نمایش هدر «{source.strip()}» دوباره فعال شد."
        if changed else f"هدر «{source.strip()}» در فهرست هدرهای مخفی نبود."
    )


async def hidden_headers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    connection = connect(settings.database_path, readonly=True)
    try:
        headers = list_hidden_headers(connection)
    finally:
        connection.close()
    if not headers:
        await update.effective_message.reply_text("هیچ هدری مخفی نشده است.")
        return
    await update.effective_message.reply_text(
        "هدرهای مخفی‌شده (نام اصلی ستون):\n" + "\n".join(f"• {header}" for header in headers)
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    LOGGER.info(
        "operation=stats stage=started user_id=%s",
        update.effective_user.id,
    )
    status = await update.effective_message.reply_text(progress_text(
        "📊 دریافت آمار پایگاه‌داده",
        "آماده‌سازی خواندن آمار",
        0,
    ))
    try:
        await status.edit_text(progress_text(
            "📊 دریافت آمار پایگاه‌داده",
            "در حال خواندن وضعیت و اندازهٔ دیتابیس",
            50,
        ))
        statistics = await asyncio.to_thread(run_stats, settings)
    except Exception as exc:
        _log_exception_redacted(
            "operation=stats stage=failed user_id=%s error_type=%s"
            % (update.effective_user.id, type(exc).__name__),
            exc,
            settings.telegram_bot_token,
        )
        await status.edit_text(progress_text(
            "❌ دریافت آمار ناموفق بود",
            "جزئیات در CMD ثبت شد",
            100,
        ))
        return
    size_mib = float(statistics["database_bytes"]) / (1024 * 1024)
    LOGGER.info(
        "operation=stats stage=complete records=%s completed_files=%s failed_files=%s database_bytes=%s",
        statistics["records"], statistics["completed_files"],
        statistics["failed_files"], statistics["database_bytes"],
    )
    await status.edit_text(
        progress_text("✅ آمار آماده شد", "پایان", 100)
        + "\n\n<b>آمار پایگاه داده</b>\n"
        f"تعداد کل ردیف‌ها: {statistics['records']:,}\n"
        f"فایل‌های کامل‌شده: {statistics['completed_files']:,}\n"
        f"در حال پردازش: {statistics['importing_files']:,}\n"
        f"فایل‌های ناموفق: {statistics['failed_files']:,}\n"
        f"حجم پایگاه داده: {size_mib:,.1f} MiB\n"
        f"آخرین ورود: {escape(str(statistics['last_import']))}\n"
        f"ستون‌های جست‌وجو: ستون‌های انتخاب‌شده‌ی هر فایل\n"
        f"شاخص‌های جست‌وجو آماده‌اند: {'بله' if statistics['search_indexes_ready'] else 'خیر'}",
        parse_mode=ParseMode.HTML,
    )
async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Capture unhandled update/task failures with enough context to diagnose them."""
    error = context.error
    if error is None:
        return
    settings = context.application.bot_data.get("settings")
    effective_user = getattr(update, "effective_user", None)
    effective_chat = getattr(update, "effective_chat", None)
    update_id = getattr(update, "update_id", "-")
    user_id = getattr(effective_user, "id", "-")
    chat_id = getattr(effective_chat, "id", "-")
    _log_exception_redacted(
        "operation=telegram_update stage=unhandled_exception update_id=%s user_id=%s chat_id=%s error_type=%s"
        % (update_id, user_id, chat_id, type(error).__name__),
        error,
        getattr(settings, "telegram_bot_token", None),
    )


async def log_update_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    LOGGER.info(
        "operation=telegram_update stage=received update_id=%s user_id=%s chat_id=%s chat_type=%s update_kind=%s",
        update.update_id,
        getattr(user, "id", "-"),
        getattr(chat, "id", "-"),
        getattr(chat, "type", "-"),
        "document" if message and message.document else "text" if message and message.text else "callback" if update.callback_query else "other",
    )


async def log_update_completed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    LOGGER.info(
        "operation=telegram_update stage=handler_completed update_id=%s user_id=%s chat_id=%s",
        update.update_id, getattr(user, "id", "-"), getattr(chat, "id", "-"),
    )


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    callback = update.callback_query
    if callback is None:
        return
    if not authorized(update, settings):
        await callback.answer("دسترسی مجاز نیست.", show_alert=True)
        return
    try:
        _, token, page_value = (callback.data or "").split(":", 2)
        page = int(page_value)
        sessions: dict[str, QuerySession] = context.user_data.get(SESSION_KEY, {})
        session = sessions[token]
    except (KeyError, ValueError):
        await callback.answer("این صفحه‌ی نتیجه منقضی شده است؛ دوباره جست‌وجو کنید.", show_alert=True)
        return
    await callback.answer()
    LOGGER.info(
        "operation=pagination stage=started user_id=%s page=%d query_length=%d",
        update.effective_user.id, page + 1, len(session.query),
    )
    await callback.edit_message_text(progress_text(
        "📄 صفحه‌بندی نتیجه‌ها",
        "آماده‌سازی صفحهٔ بعد",
        0,
    ))
    try:
        await callback.edit_message_text(progress_text(
            "📄 صفحه‌بندی نتیجه‌ها",
            "در حال جست‌وجوی صفحه در دیتابیس",
            35,
        ))
        result = await asyncio.to_thread(run_search, settings, session.query, page)
    except Exception as exc:
        _log_exception_redacted(
            "operation=pagination stage=search_failed user_id=%s page=%d error_type=%s"
            % (update.effective_user.id, page + 1, type(exc).__name__),
            exc,
            settings.telegram_bot_token,
        )
        await callback.edit_message_text(progress_text(
            "❌ بارگذاری صفحه ناموفق بود",
            "جزئیات در CMD ثبت شد",
            100,
        ))
        return
    visible_total = min(result.total_count, settings.max_results)
    if not result.rows or page * settings.results_per_page >= visible_total:
        await callback.answer("نتیجه‌ی دیگری نیست.", show_alert=True)
        return
    aliases, hidden_headers = await asyncio.gather(
        asyncio.to_thread(load_display_header_aliases, settings),
        asyncio.to_thread(load_hidden_header_keys, settings),
    )
    completion_text = progress_text("✅ صفحه آماده شد", "در حال نمایش نتیجه‌ها", 100)
    page_budget = max(100, 3900 - len(completion_text) - 1)
    needs_full_csv = page_needs_full_csv(
        result,
        settings,
        hidden_headers,
        aliases,
        query=session.query,
        page=page,
        max_chars=page_budget,
    )
    LOGGER.info(
        "operation=pagination stage=query_complete page=%d results=%d full_csv=%s",
        page + 1, len(result.rows), needs_full_csv,
    )
    await callback.edit_message_text(
        completion_text + "\n"
        + format_page(
            session.query,
            page,
            result,
            settings,
            full_csv_attached=needs_full_csv,
            header_aliases=aliases,
            hidden_headers=hidden_headers,
            max_chars=page_budget,
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=pagination_keyboard(token, page, result.total_count, settings),
        disable_web_page_preview=True,
    )
    if needs_full_csv:
        LOGGER.info(
            "operation=pagination stage=full_csv_send_started page=%d",
            page + 1,
        )
        await context.bot.send_document(
            chat_id=callback.message.chat_id,
            document=io.BytesIO(
                full_results_csv_bytes(session.query, page, result, aliases, hidden_headers)
            ),
            filename=f"نتایج_جست‌وجو_{page + 1}.csv",
            caption="همه‌ی فیلدهای اصلی ردیف‌های نتیجه.",
        )
        LOGGER.info(
            "operation=pagination stage=full_csv_send_complete page=%d",
            page + 1,
        )
def main() -> None:
    global LOGGER
    instance_lock = None
    try:
        settings = load_settings()
        LOGGER = configure_logging(
            settings.log_directory,
            "bot.log",
            settings.log_level,
            secrets_to_redact=(settings.telegram_bot_token,),
        )
        LOGGER.info("operation=startup stage=settings_loaded")
        settings.validate_for_bot()
        LOGGER.info("operation=startup stage=settings_validated")
        try:
            instance_lock = acquire_instance_lock(
                settings.data_directory / ".telegram-csv-search.lock"
            )
        except BotInstanceAlreadyRunning as exc:
            LOGGER.error("operation=startup stage=instance_lock_failed detail=%s", exc)
            raise SystemExit(str(exc)) from exc
        LOGGER.info("operation=startup stage=instance_lock_acquired")
        # A fresh Railway/container deployment may not have a CSV or database yet.
        # Create the empty schema first so the bot can start and receive its first
        # CSV through Telegram upload or the configured Google Drive link flow.
        LOGGER.info("operation=startup stage=database_bootstrap_started path=%s", settings.database_path)
        connection = connect(settings.database_path)
        try:
            initialize_database(connection)
            initialize_access_control(connection, settings)
        finally:
            connection.close()
        LOGGER.info("operation=startup stage=database_bootstrap_complete")
    except ConfigurationError as exc:
        if LOGGER.handlers:
            LOGGER.error("operation=startup stage=configuration_failed error=%s", exc)
        raise SystemExit(f"Configuration error: {exc}")
    except Exception:
        if LOGGER.handlers:
            LOGGER.exception("operation=startup stage=failed")
        raise
    LOGGER.info(
        "operation=startup stage=bot_starting bootstrap_user_count=%d admin_count=%d",
        len(settings.allowed_user_ids), len(settings.admin_user_ids),
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .base_url(settings.telegram_base_url)
        .base_file_url(settings.telegram_file_base_url)
        .build()
    )
    LOGGER.info("operation=startup stage=application_built")
    application.bot_data["settings"] = settings
    manager = UploadManager(settings, application.bot)
    application.bot_data["upload_manager"] = manager
    search_queue = SearchQueue(process_queued_search)
    application.bot_data[SEARCH_QUEUE_KEY] = search_queue
    application.add_handler(TypeHandler(Update, search_busy_guard), group=-2)
    application.add_handler(TypeHandler(Update, log_update_received), group=-1)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("database", database_management_command))
    application.add_handler(CommandHandler("admin", admin_panel_command))
    application.add_handler(CommandHandler("admin_add", admin_add_command))
    application.add_handler(CommandHandler("admin_remove", admin_remove_command))
    application.add_handler(CommandHandler("user_add", user_add_command))
    application.add_handler(CommandHandler("user_remove", user_remove_command))
    application.add_handler(CommandHandler("access", access_command))
    application.add_handler(CommandHandler("header_set", header_set_command))
    application.add_handler(CommandHandler("header_aliases", header_aliases_command))
    application.add_handler(CommandHandler("header_remove", header_remove_command))
    application.add_handler(CommandHandler("header_hide", header_hide_command))
    application.add_handler(CommandHandler("header_show", header_show_command))
    application.add_handler(CommandHandler("header_hidden", hidden_headers_command))
    application.add_handler(CommandHandler("drive_setup", drive_setup_command))
    application.add_handler(CallbackQueryHandler(database_management_callback, pattern=r"^db:"))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    application.add_handler(CallbackQueryHandler(upload_selection_callback, pattern=r"^u:"))
    application.add_handler(CallbackQueryHandler(page_callback, pattern=r"^p:[A-Za-z0-9_-]+:[0-9]+$"))
    application.add_handler(MessageHandler(filters.Document.ALL, document_upload))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Regex(
            r"https?://(?:drive\.google\.com|docs\.google\.com|drive\.usercontent\.google\.com|drive\.googleusercontent\.com)/"
        ),
        google_drive_link_upload,
    ))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_search))
    application.add_error_handler(telegram_error_handler)
    application.add_handler(TypeHandler(Update, log_update_completed), group=100)
    LOGGER.info("operation=startup stage=handlers_registered")

    async def post_init(app: Application) -> None:
        LOGGER.info("operation=startup stage=post_init_started")
        await manager.start()
        LOGGER.info("operation=startup stage=upload_manager_started")
        await _sync_bot_command_menus(app.bot, settings)
        LOGGER.info("operation=startup stage=telegram_command_menus_synchronized")

    async def post_shutdown(app: Application) -> None:
        LOGGER.info("operation=shutdown stage=search_queue_stopping")
        await search_queue.stop()
        LOGGER.info("operation=shutdown stage=search_queue_stopped")
        LOGGER.info("operation=shutdown stage=upload_manager_stopping")
        await manager.stop()
        LOGGER.info("operation=shutdown stage=upload_manager_stopped")

    application.post_init = post_init
    application.post_shutdown = post_shutdown
    try:
        LOGGER.info("operation=startup stage=polling_starting")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
        LOGGER.info("operation=shutdown stage=polling_stopped")
    finally:
        if instance_lock is not None:
            instance_lock.close()
            LOGGER.info("operation=shutdown stage=instance_lock_released")


if __name__ == "__main__":
    main()
