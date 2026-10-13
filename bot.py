#!/usr/bin/env python3
"""Authorized Telegram interface for the SQLite CSV search database."""
from __future__ import annotations

import asyncio
import csv
from dataclasses import dataclass, replace
from html import escape
import io
import json
import logging
import math
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ConfigurationError, Settings, load_settings
from database import (
    access_mode as db_access_mode,
    add_bot_user,
    bot_user_exists,
    connect,
    database_statistics,
    delete_header_alias,
    get_header_aliases,
    header_alias_key,
    initialize_access_control,
    initialize_database,
    list_bot_users,
    list_header_alias_entries,
    remove_bot_user,
    set_access_mode,
    set_header_alias,
)
from logging_utils import configure_logging
from search import SearchPage, parse_raw_fields, search_records
from upload_manager import UploadJob, UploadManager, safe_upload_path
from import_csv import read_csv_header

LOGGER = logging.getLogger("telegram_csv_search")
SESSION_KEY = "search_sessions"
UPLOAD_SELECTION_KEY = "pending_upload_selection"
MAX_SESSIONS_PER_USER = 25
UPLOAD_HEADER_PAGE_SIZE = 8
SEARCH_PREVIEW_LIMIT = 5
SEARCH_EXPORT_PAGE_SIZE = 1000
SEARCH_EXPORT_CHUNK_BYTES = 40 * 1024 * 1024


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


def is_admin(user_id: int | None, settings: Settings) -> bool:
    return user_id is not None and user_id in settings.admin_user_ids


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


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 1)].rstrip() + "…"


def format_record(
    row: Any,
    index: int,
    settings: Settings,
    header_aliases: dict[str, str] | None = None,
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
    values = [
        (
            (header_aliases or {}).get(
                header_alias_key(str(headers[i]) if i < len(headers) else f"Column {i + 1}"),
                str(headers[i]) if i < len(headers) else f"Column {i + 1}",
            ),
            raw_fields[i] if i < len(raw_fields) else "",
        )
        for i in range(field_count)
    ]
    source_budget = min(settings.max_field_display_chars, 315)
    allocation = max(24, source_budget // max(len(values) + 1, 1))
    lines = [f"<b>{index}.</b>"]
    for label, value in values:
        rendered = truncate(value, allocation) if value else "—"
        lines.append(f"<b>{escape(label)}:</b> {escape(rendered)}")
    lines.append(f"<b>منبع:</b> {escape(str(row['source_file']))}:{row['source_line']}")
    return "\n".join(lines)


def format_full_text_record(
    row: Any, index: int, header_aliases: dict[str, str] | None = None
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
    lines: list[str] = []
    for i in range(max(len(headers), len(fields))):
        original = str(headers[i]) if i < len(headers) else f"column_{i + 1}"
        label = aliases.get(header_alias_key(original), original)
        value = fields[i] if i < len(fields) else ""
        lines.append(f"{label}: {value}" if value else f"{label}: —")
    lines.append(f"منبع: {row['source_file']}:{row['source_line']}")
    return "\n".join(lines) + "\n\n"


def format_search_preview(
    query: str,
    result: SearchPage,
    settings: Settings,
    header_aliases: dict[str, str] | None = None,
) -> str:
    mode_label = {"exact": "دقیق", "prefix": "ابتدای مقدار", "text": "متنی"}.get(result.mode, "جست‌وجو")
    lines = [
        f"<b>عبارت جست‌وجو:</b> {escape(truncate(query, settings.max_query_length))}",
        f"<b>نوع جست‌وجو:</b> {mode_label}",
        f"<b>تعداد کل نتیجه‌ها:</b> {result.total_count:,}",
        "",
    ]
    rows = list(result.rows[:SEARCH_PREVIEW_LIMIT])
    lines.extend(format_record(row, i + 1, settings, header_aliases) for i, row in enumerate(rows))
    if result.total_count > SEARCH_PREVIEW_LIMIT:
        lines.append("\n<b>نمایش پیام:</b> ۵ نتیجه‌ی اول؛ فایل متنی شامل همه‌ی نتیجه‌هاست.")
    text = "\n\n".join(lines)
    return text if len(text) <= 3900 else text[:3899].rsplit("\n", 1)[0] + "\n…"


def write_search_text_exports(
    settings: Settings,
    query: str,
    destination: Path,
    header_aliases: dict[str, str] | None = None,
    *,
    page_size: int = SEARCH_EXPORT_PAGE_SIZE,
    max_chunk_bytes: int = SEARCH_EXPORT_CHUNK_BYTES,
) -> list[Path]:
    """Stream every match to bounded UTF-8 TXT chunks without truncating values."""
    if page_size < 1 or max_chunk_bytes < 4:
        raise ValueError("page_size must be positive and max_chunk_bytes must be at least 4.")
    export_settings = replace(settings, results_per_page=page_size)
    first = run_search(export_settings, query, 0)
    total = first.total_count
    if total <= 0:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    written = 0
    handle = None

    def open_chunk(number: int):
        path = destination / f"نتایج_جست‌وجو_{number:03d}.txt"
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
        for page in range(math.ceil(total / page_size)):
            result = first if page == 0 else run_search(
                export_settings, query, page, mode=first.mode, count_total=False
            )
            for offset, row in enumerate(result.rows):
                record = format_full_text_record(
                    row, page * page_size + offset + 1, header_aliases
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
        if not paths:
            raise RuntimeError("Search count was positive but no rows were returned.")
    finally:
        if handle is not None:
            handle.close()
    return paths



def page_needs_full_csv(result: SearchPage, settings: Settings) -> bool:
    """Return true if the chat preview would shorten values or exceed Telegram limits."""
    if len(format_page("", 0, result, settings)) > 3900:
        return True
    for row in result.rows:
        raw_fields = parse_raw_fields(row)
        try:
            headers = json.loads(row["headers_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            headers = []
        field_count = max(len(headers), len(raw_fields))
        source_budget = min(settings.max_field_display_chars, 315)
        allocation = max(24, source_budget // max(field_count + 1, 1))
        if any(len(value) > allocation for value in raw_fields if value):
            return True
    return False


def full_results_csv_bytes(query: str, page: int, result: SearchPage) -> bytes:
    """Serialize every original field on the current page for complete delivery."""
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["query", query, "page", page + 1])
    for number, row in enumerate(result.rows, start=1):
        try:
            headers = json.loads(row["headers_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            headers = []
        fields = parse_raw_fields(row)
        if not headers:
            headers = [f"column_{index + 1}" for index in range(len(fields))]
        writer.writerow(["result", number, "source", row["source_file"], "line", row["source_line"]])
        writer.writerow([str(value) for value in headers])
        writer.writerow(fields)
        writer.writerow([])
    return output.getvalue().encode("utf-8-sig")


def format_page(
    query: str,
    page: int,
    result: SearchPage,
    settings: Settings,
    *,
    full_csv_attached: bool = False,
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
    header += f"<b>نمایش:</b> {start:,} تا {end:,}\n\n"
    records = [
        format_record(row, start + offset, settings) for offset, row in enumerate(result.rows)
    ]
    # The construction budgets each record; this final cap additionally protects unusual
    # escape expansion. Result metadata remains intact if a pathological row is encountered.
    message = header + "\n\n".join(records)
    return message if len(message) <= 3900 else message[:3899].rsplit("\n", 1)[0] + "\n…"


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
    started = time.perf_counter()
    try:
        result = await asyncio.to_thread(run_search, settings, query, 0, SEARCH_PREVIEW_LIMIT)
        aliases = await asyncio.to_thread(load_display_header_aliases, settings)
    except Exception:
        LOGGER.exception("Search failed for user id=%s", update.effective_user.id)
        await update.effective_message.reply_text("جست‌وجو موقتاً در دسترس نیست؛ لطفاً کمی بعد دوباره تلاش کنید.")
        return
    elapsed_ms = (time.perf_counter() - started) * 1000
    LOGGER.info(
        "Search completed user_id=%s query_length=%d mode=%s results=%d duration_ms=%.1f",
        update.effective_user.id, len(query), result.mode, result.total_count, elapsed_ms,
    )
    if not result.total_count:
        await update.effective_message.reply_text("نتیجه‌ای پیدا نشد. عبارت دقیق‌تر یا کامل‌تری را امتحان کنید.")
        return

    await update.effective_message.reply_text(
        format_search_preview(query, result, settings, aliases),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    needs_txt = result.total_count > SEARCH_PREVIEW_LIMIT or page_needs_full_csv(result, settings)
    if not needs_txt:
        return

    status = await update.effective_message.reply_text(
        f"در حال آماده‌سازی فایل متنیِ کامل برای {result.total_count:,} نتیجه…"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="telegram-search-results-") as temp_dir:
            paths = await asyncio.to_thread(
                write_search_text_exports, settings, query, Path(temp_dir), aliases
            )
            await status.edit_text(f"فایل کامل آماده شد؛ {result.total_count:,} نتیجه در {len(paths)} فایل متنی قرار گرفت.")
            for part_number, path in enumerate(paths, start=1):
                with path.open("rb") as document:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=document,
                        filename=path.name,
                        caption=f"بخش {part_number} از {len(paths)}؛ همه‌ی فیلدهای کامل ردیف‌های منطبق.",
                    )
    except Exception:
        LOGGER.exception("Could not create/send full text search results user_id=%s", update.effective_user.id)
        await status.edit_text("ساخت یا ارسال فایل کامل نتیجه‌ها ناموفق بود؛ لطفاً دوباره تلاش کنید.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    await update.effective_message.reply_text(
        "یک عبارت بفرستید تا در ستون‌های قابل جست‌وجوی فایل‌های CSV جست‌وجو شود. "
        "در پیام حداکثر ۵ نتیجه می‌آید و اگر نتیجه‌های بیشتری باشد، فایل متنی کامل فرستاده می‌شود.\n\n"
        "دستورها: /search <عبارت>، /stats، /help، /admin"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    await update.effective_message.reply_text(
        "برای جست‌وجو یک مقدار بفرستید یا از /search استفاده کنید. هنگام بارگذاری CSV ستون‌های جست‌وجو را انتخاب می‌کنید؛ نتیجه همه‌ی فیلدهای ردیف را دارد. "
        "مدیر: /admin، /user_add، /user_remove، /access، /header_set و /header_aliases."
    )


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    await respond_to_search(update, context, settings, " ".join(context.args))


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
) -> None:
    """Stream a Telegram file to disk and edit one status message periodically.

    A Local Bot API Server is required for files larger than Telegram's hosted
    Bot API download limit. The configured file base URL is used deliberately so
    both cloud and local Bot API deployments work.
    """
    if not file_path:
        raise RuntimeError("تلگرام مسیر قابل دریافت فایل را برنگرداند.")
    local_source = Path(file_path)
    if local_source.is_absolute():
        if not local_source.is_file():
            raise FileNotFoundError(
                "Local Bot API returned a filesystem path that is not visible to the bot process; "
                "mount the Bot API data directory into the bot container."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        total = total_bytes or local_source.stat().st_size
        started = time.monotonic()
        received = 0
        last_update = 0.0
        with local_source.open("rb") as source, destination.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                received += len(chunk)
                now = time.monotonic()
                if now - last_update >= settings.upload_progress_interval_seconds:
                    elapsed = max(now - started, 0.001)
                    percent = received / total * 100 if total else 0.0
                    text = (
                        f"⬇️ دریافت فایل\n{filename}\n"
                        f"{_progress_bar(percent)} {percent:5.1f}%\n"
                        f"حجم: {_human_bytes(received)} / {_human_bytes(total)}\n"
                        f"سرعت: {_human_bytes(received / elapsed)} در ثانیه"
                    )
                    try:
                        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                    except Exception:
                        LOGGER.exception("Could not update local download progress filename=%s", filename)
                    last_update = now
        return
    url = telegram_file_download_url(settings, file_path)
    started = time.monotonic()
    last_update = 0.0
    received = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                async for chunk in response.aiter_bytes(chunk_size=1024 * 1024):
                    output.write(chunk)
                    received += len(chunk)
                    now = time.monotonic()
                    if now - last_update >= settings.upload_progress_interval_seconds:
                        elapsed = max(now - started, 0.001)
                        percent = received / total_bytes * 100 if total_bytes else 0.0
                        text = (
                            f"⬇️ دریافت فایل\n{filename}\n"
                            f"{_progress_bar(percent)} {percent:5.1f}%\n"
                            f"حجم: {_human_bytes(received)}"
                            + (f" / {_human_bytes(total_bytes)}" if total_bytes else "")
                            + f"\nسرعت: {_human_bytes(received / elapsed)} در ثانیه"
                        )
                        try:
                            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
                        except Exception:
                            LOGGER.exception("Could not update download progress filename=%s", filename)
                        last_update = now


async def text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    await respond_to_search(update, context, settings, update.effective_message.text or "")


def upload_selection_keyboard(session: UploadSelectionSession) -> InlineKeyboardMarkup:
    start = session.page * UPLOAD_HEADER_PAGE_SIZE
    end = min(start + UPLOAD_HEADER_PAGE_SIZE, len(session.headers))
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(start, end):
        checked = index in session.selected
        label = truncate(session.headers[index], 42)
        rows.append([InlineKeyboardButton(
            f"{'☑' if checked else '☐'} {label}",
            callback_data=f"u:{session.token}:t:{index}",
        )])
    navigation: list[InlineKeyboardButton] = []
    if session.page > 0:
        navigation.append(InlineKeyboardButton("← قبلی", callback_data=f"u:{session.token}:p:{session.page - 1}"))
    if end < len(session.headers):
        navigation.append(InlineKeyboardButton("بعدی →", callback_data=f"u:{session.token}:p:{session.page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([
        InlineKeyboardButton("✅ تأیید انتخاب", callback_data=f"u:{session.token}:done"),
        InlineKeyboardButton("لغو", callback_data=f"u:{session.token}:cancel"),
    ])
    return InlineKeyboardMarkup(rows)


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
        await callback.answer()
        await callback.edit_message_reply_markup(reply_markup=upload_selection_keyboard(session))
        return
    if action == "cancel":
        sessions.pop(UPLOAD_SELECTION_KEY, None)
        session.job.path.unlink(missing_ok=True)
        await callback.answer("فایل لغو شد.")
        await callback.edit_message_text("بارگذاری و ورود اطلاعات لغو شد؛ فایل موقت حذف شد.")
        return
    if action == "done":
        if not session.selected:
            await callback.answer("حداقل یک ستون را برای جست‌وجو انتخاب کنید.", show_alert=True)
            return
        job = replace(session.job, selected_column_indices=tuple(sorted(session.selected)))
        manager: UploadManager = context.application.bot_data["upload_manager"]
        position = await manager.enqueue(job)
        sessions.pop(UPLOAD_SELECTION_KEY, None)
        await callback.answer("انتخاب ثبت شد.")
        await callback.edit_message_text(
            f"✅ ستون‌های انتخابی ثبت شدند\n{job.original_filename}\n"
            f"📥 در صف ورود اطلاعات قرار گرفت؛ موقعیت: {position}\n"
            "جست‌وجو فقط در ستون‌های انتخاب‌شده انجام می‌شود؛ همه‌ی فیلدهای ردیف نتیجه نمایش داده می‌شوند."
        )
        return
    await callback.answer("عملیات ناشناخته است.", show_alert=True)


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
    # This path is inside DATA_DIRECTORY and remains hidden from discovery until the
    # download is complete and atomically renamed.
    settings.data_directory.mkdir(parents=True, exist_ok=True)
    temporary_path = safe_upload_path(settings.data_directory, filename)
    final_path = temporary_path.with_suffix("")
    status_message = await message.reply_text(f"📦 فایل دریافت شد\n{filename}\n⏳ آماده‌سازی دریافت…")
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await download_file_with_progress(
            settings,
            telegram_file.file_path,
            temporary_path,
            context.bot,
            status_message.chat_id,
            status_message.message_id,
            filename,
            document.file_size,
        )
        actual_size = temporary_path.stat().st_size
        if actual_size > settings.upload_max_bytes:
            temporary_path.unlink(missing_ok=True)
            await status_message.edit_text(
                f"❌ حجم فایل ({actual_size:,} بایت) از سقف تنظیم‌شده ({settings.upload_max_bytes:,} بایت) بیشتر است؛ فایل حذف شد."
            )
            return
        temporary_path.replace(final_path)
    except Exception as exc:
        # HTTP client exceptions include the request URL; never emit the bot token
        # through a traceback or exception string.
        detail = str(exc).replace(settings.telegram_bot_token, "[REDACTED]")
        LOGGER.error(
            "Telegram download failed user_id=%s filename=%s error_type=%s detail=%s",
            update.effective_user.id, filename, type(exc).__name__, detail,
        )
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not remove incomplete upload path=%s", temporary_path)
        await status_message.edit_text("❌ دریافت فایل ناموفق بود. فایل ناقص حذف شد و ورود اطلاعات شروع نشد.")
        return
    try:
        headers = await asyncio.to_thread(read_csv_header, final_path, settings)
    except Exception as exc:
        LOGGER.exception("Could not read CSV header user_id=%s filename=%s", update.effective_user.id, filename)
        final_path.unlink(missing_ok=True)
        await status_message.edit_text(
            "❌ خواندن سرستون‌های فایل ناموفق بود؛ فایل حذف شد و ورود اطلاعات شروع نشد."
        )
        return
    if context.user_data.get(UPLOAD_SELECTION_KEY):
        final_path.unlink(missing_ok=True)
        await status_message.edit_text("❌ برای این حساب انتخاب ستونی باز است. ابتدا آن را تأیید یا لغو کنید، سپس فایل بعدی را بفرستید.")
        return
    job = UploadJob(
        path=final_path,
        original_filename=filename,
        file_id=document.file_id,
        file_unique_id=document.file_unique_id,
        file_size=document.file_size,
        uploader_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        status_message_id=status_message.message_id,
    )
    selection = UploadSelectionSession(
        token=secrets.token_urlsafe(6),
        job=job,
        headers=tuple(headers),
        selected=set(),
    )
    context.user_data[UPLOAD_SELECTION_KEY] = selection
    await status_message.edit_text(
        f"✅ دریافت فایل کامل شد\n{filename}\n"
        f"ستون‌هایی را که باید در جست‌وجو باشند انتخاب کنید ({len(headers)} ستون).\n"
        "برای نمایش نتیجه، تمام ستون‌های همان ردیف از CSV خوانده می‌شوند.",
        reply_markup=upload_selection_keyboard(selection),
    )


def _admin_keyboard(settings: Settings) -> InlineKeyboardMarkup:
    mode, users = access_state(settings)
    mode_label = "🔓 عمومی‌کردن ربات" if mode == "private" else "🔒 خصوصی‌کردن ربات"
    rows: list[list[InlineKeyboardButton]] = [[
        InlineKeyboardButton(mode_label, callback_data=f"adm:mode:{'public' if mode == 'private' else 'private'}")
    ]]
    removable = [user_id for user_id in users if user_id not in settings.admin_user_ids]
    for start in range(0, min(len(removable), 40), 2):
        rows.append([
            InlineKeyboardButton(f"حذف {user_id}", callback_data=f"adm:remove:{user_id}")
            for user_id in removable[start:start + 2]
        ])
    return InlineKeyboardMarkup(rows)


def _admin_panel_text(settings: Settings) -> str:
    mode, users = access_state(settings)
    mode_text = "عمومی؛ جست‌وجو برای همه باز است" if mode == "public" else "خصوصی؛ فقط کاربران مجاز"
    removable_count = sum(user_id not in settings.admin_user_ids for user_id in users)
    return (
        "پنل مدیریت\n"
        f"وضعیت دسترسی: {mode_text}\n"
        f"کاربران ثبت‌شده: {len(users)} (قابل حذف از دکمه‌ها: {min(removable_count, 40)})\n\n"
        "افزودن کاربر: /user_add 123456789\n"
        "حذف کاربر: /user_remove 123456789\n"
        "تغییر حالت: /access private یا /access public\n"
        "تغییر نام هدر سراسری: /header_set NATIONAL_CODE = کد ملی\n"
        "دیدن نام‌های هدر: /header_aliases\n"
        "بازگرداندن نام اصلی هدر: /header_remove NATIONAL_CODE\n\n"
        "حالت عمومی فقط جست‌وجو را برای همه باز می‌کند؛ بارگذاری فایل همچنان فقط برای کاربران مجاز است."
    )


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


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await _deny_non_admin(update, settings):
        return
    await update.effective_message.reply_text(
        _admin_panel_text(settings), reply_markup=_admin_keyboard(settings)
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    callback = update.callback_query
    if callback is None:
        return
    if not update.effective_chat or update.effective_chat.type != "private":
        await callback.answer("این پنل فقط در گفت‌وگوی خصوصی ربات کار می‌کند.", show_alert=True)
        return
    if not is_admin(update.effective_user.id if update.effective_user else None, settings):
        await callback.answer("فقط مدیر مجاز است.", show_alert=True)
        return
    parts = (callback.data or "").split(":")
    if len(parts) != 3:
        await callback.answer("درخواست نامعتبر است.", show_alert=True)
        return
    action, value = parts[1], parts[2]
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
        if user_id in settings.admin_user_ids:
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
        _admin_panel_text(settings), reply_markup=_admin_keyboard(settings)
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
    if user_id in settings.admin_user_ids:
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


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    try:
        statistics = await asyncio.to_thread(run_stats, settings)
    except Exception:
        LOGGER.exception("Stats request failed for user id=%s", update.effective_user.id)
        await update.effective_message.reply_text("آمار موقتاً در دسترس نیست.")
        return
    size_mib = float(statistics["database_bytes"]) / (1024 * 1024)
    await update.effective_message.reply_text(
        "<b>آمار پایگاه داده</b>\n"
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
    try:
        result = await asyncio.to_thread(run_search, settings, session.query, page)
    except Exception:
        LOGGER.exception("Pagination search failed for user id=%s", update.effective_user.id)
        await callback.edit_message_text("جست‌وجو موقتاً در دسترس نیست؛ دوباره تلاش کنید.")
        return
    visible_total = min(result.total_count, settings.max_results)
    if not result.rows or page * settings.results_per_page >= visible_total:
        await callback.answer("نتیجه‌ی دیگری نیست.", show_alert=True)
        return
    needs_full_csv = page_needs_full_csv(result, settings)
    await callback.edit_message_text(
        format_page(session.query, page, result, settings, full_csv_attached=needs_full_csv),
        parse_mode=ParseMode.HTML,
        reply_markup=pagination_keyboard(token, page, result.total_count, settings),
        disable_web_page_preview=True,
    )
    if needs_full_csv:
        await context.bot.send_document(
            chat_id=callback.message.chat_id,
            document=io.BytesIO(full_results_csv_bytes(session.query, page, result)),
            filename=f"نتایج_جست‌وجو_{page + 1}.csv",
            caption="همه‌ی فیلدهای اصلی ردیف‌های نتیجه.",
        )


def main() -> None:
    try:
        settings = load_settings()
        # A fresh Railway/container deployment may not have a CSV or database yet.
        # Create the empty schema first so the bot can start and receive its first
        # CSV through Telegram upload.
        connection = connect(settings.database_path)
        try:
            initialize_database(connection)
            initialize_access_control(connection, settings)
        finally:
            connection.close()
        settings.validate_for_bot()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}")
    global LOGGER
    LOGGER = configure_logging(settings.log_directory, "bot.log", settings.log_level)
    LOGGER.info(
        "Bot starting; bootstrap user count=%d admin count=%d",
        len(settings.allowed_user_ids), len(settings.admin_user_ids),
    )

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .base_url(settings.telegram_base_url)
        .base_file_url(settings.telegram_file_base_url)
        .build()
    )
    application.bot_data["settings"] = settings
    manager = UploadManager(settings, application.bot)
    application.bot_data["upload_manager"] = manager
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("admin", admin_panel_command))
    application.add_handler(CommandHandler("user_add", user_add_command))
    application.add_handler(CommandHandler("user_remove", user_remove_command))
    application.add_handler(CommandHandler("access", access_command))
    application.add_handler(CommandHandler("header_set", header_set_command))
    application.add_handler(CommandHandler("header_aliases", header_aliases_command))
    application.add_handler(CommandHandler("header_remove", header_remove_command))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^adm:"))
    application.add_handler(CallbackQueryHandler(upload_selection_callback, pattern=r"^u:"))
    application.add_handler(CallbackQueryHandler(page_callback, pattern=r"^p:[A-Za-z0-9_-]+:[0-9]+$"))
    application.add_handler(MessageHandler(filters.Document.ALL, document_upload))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_search))

    async def post_init(app: Application) -> None:
        await manager.start()

    async def post_shutdown(app: Application) -> None:
        await manager.stop()

    application.post_init = post_init
    application.post_shutdown = post_shutdown
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
