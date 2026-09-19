#!/usr/bin/env python3
"""Authorized Telegram interface for the SQLite CSV search database."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from html import escape
import json
import logging
from pathlib import Path
import secrets
import time
from typing import Any

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
from database import connect, database_statistics, initialize_database
from logging_utils import configure_logging
from search import SearchPage, parse_raw_fields, search_records
from upload_manager import UploadJob, UploadManager, safe_upload_path

LOGGER = logging.getLogger("telegram_csv_search")
SESSION_KEY = "search_sessions"
MAX_SESSIONS_PER_USER = 25


@dataclass(frozen=True, slots=True)
class QuerySession:
    query: str


def authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    return user is not None and user.id in settings.allowed_user_ids


async def deny_if_unauthorized(update: Update, settings: Settings) -> bool:
    if authorized(update, settings):
        return False
    user_id = update.effective_user.id if update.effective_user else None
    LOGGER.warning("Denied bot access for Telegram user id=%s", user_id)
    if update.effective_message:
        await update.effective_message.reply_text("Access denied.")
    return True


def truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 1)].rstrip() + "…"


def format_record(row: Any, index: int, settings: Settings) -> str:
    """Render only non-empty values using the source file's real headers."""
    try:
        headers = json.loads(row["headers_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        headers = []
    raw_fields = parse_raw_fields(row)
    if not headers:
        headers = ["column_1", "column_2", "name", "birth_date", "phone"]
    values = [(str(headers[i]) if i < len(headers) else f"Column {i + 1}", value)
              for i, value in enumerate(raw_fields) if value]
    source_budget = min(settings.max_field_display_chars, 315)
    allocation = max(24, source_budget // max(len(values) + 1, 1))
    lines = [f"<b>{index}.</b>"]
    for label, value in values:
        lines.append(f"<b>{escape(label)}:</b> {escape(truncate(value, allocation))}")
    lines.append(f"<b>Source:</b> {escape(str(row['source_file']))}:{row['source_line']}")
    return "\n".join(lines)


def format_page(query: str, page: int, result: SearchPage, settings: Settings) -> str:
    visible_total = min(result.total_count, settings.max_results)
    start = page * settings.results_per_page + 1
    end = min(start + len(result.rows) - 1, visible_total)
    mode_label = {"exact": "exact", "prefix": "prefix", "text": "text"}.get(result.mode, "search")
    header = (
        f"<b>Search:</b> {escape(truncate(query, settings.max_query_length))}\n"
        f"<b>Mode:</b> {mode_label}\n"
    )
    if result.total_count > settings.max_results:
        header += (
            f"<b>Found:</b> {result.total_count:,}; displaying the first "
            f"{settings.max_results:,} results.\n"
        )
    else:
        header += f"<b>Found:</b> {result.total_count:,}\n"
    header += f"<b>Showing:</b> {start:,}–{end:,}\n\n"
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
        buttons.append(InlineKeyboardButton("← Previous", callback_data=f"p:{token}:{page - 1}"))
    if page + 1 < pages:
        buttons.append(InlineKeyboardButton("Next →", callback_data=f"p:{token}:{page + 1}"))
    return InlineKeyboardMarkup([buttons]) if buttons else None


def run_search(settings: Settings, query: str, page: int) -> SearchPage:
    """Run a query in a worker thread with its own short-lived read-only connection."""
    connection = connect(settings.database_path, readonly=True)
    try:
        return search_records(connection, query, page, settings)
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
        await update.effective_message.reply_text("Send a phone number, ID, name, date, or other searchable value.")
        return
    if len(query) > settings.max_query_length:
        await update.effective_message.reply_text(
            f"The query is too long. Send at most {settings.max_query_length} characters."
        )
        return
    started = time.perf_counter()
    try:
        result = await asyncio.to_thread(run_search, settings, query, 0)
    except Exception:
        LOGGER.exception("Search failed for user id=%s", update.effective_user.id)
        await update.effective_message.reply_text("The search is temporarily unavailable. Please try again later.")
        return
    elapsed_ms = (time.perf_counter() - started) * 1000
    LOGGER.info(
        "Search completed user_id=%s query_length=%d mode=%s results=%d duration_ms=%.1f",
        update.effective_user.id, len(query), result.mode, result.total_count, elapsed_ms,
    )
    if not result.total_count:
        await update.effective_message.reply_text(
            "No matching records were found. Try a complete identifier or a more specific name."
        )
        return
    token = save_session(context, query)
    await update.effective_message.reply_text(
        format_page(query, 0, result, settings),
        parse_mode=ParseMode.HTML,
        reply_markup=pagination_keyboard(token, 0, result.total_count, settings),
        disable_web_page_preview=True,
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    await update.effective_message.reply_text(
        "Send a phone number, ID, name, birth date, or other searchable value. "
        "Exact matches are checked first; identifier prefixes and Persian name prefixes are indexed.\n\n"
        "Commands: /search <query>, /stats, /help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    await update.effective_message.reply_text(
        "Send a value directly or use /search. Exact phone numbers, IDs, and dates are fastest. "
        "For partial identifiers, use at least the configured prefix length. Persian names are normalized "
        "for Arabic/Persian ی and ک variants, zero-width characters, and extra spaces."
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
        raise RuntimeError("Telegram did not return a downloadable file path.")
    started = time.monotonic()
    last_update = 0.0
    received = 0
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Cloud Bot API may return a relative file path, while a Local Bot API
    # Server can return either a complete URL or an absolute local path.
    local_source = Path(file_path) if file_path.startswith("/") else None
    if local_source is not None and local_source.is_file():
        total = local_source.stat().st_size
        with local_source.open("rb") as source, destination.open("wb") as output:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
        return

    if file_path.startswith(("http://", "https://")):
        url = file_path
    else:
        url = f"{settings.telegram_file_base_url}/{settings.telegram_bot_token}/{file_path.lstrip('/')}"
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
                            f"⬇️ دانلود فایل\n{filename}\n"
                            f"{_progress_bar(percent)} {percent:5.1f}%\n"
                            f"حجم: {_human_bytes(received)}"
                            + (f" / {_human_bytes(total_bytes)}" if total_bytes else "")
                            + f"\nسرعت: {_human_bytes(received / elapsed)}/s"
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


async def document_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download an authorized private-chat CSV via the Bot API and queue its import."""
    settings: Settings = context.application.bot_data["settings"]
    message = update.effective_message
    if not message or not update.effective_chat or update.effective_chat.type != "private":
        return
    if await deny_if_unauthorized(update, settings):
        return
    if not settings.upload_enabled:
        await message.reply_text("File uploads are disabled by configuration.")
        return
    document = message.document
    if not document:
        return
    filename = document.file_name or "upload.csv"
    if Path(filename).suffix.lower() not in {".csv", ".txt"}:
        await message.reply_text("Only CSV or TXT documents are accepted.")
        return
    if document.file_size and document.file_size > settings.upload_max_bytes:
        await message.reply_text(f"This file exceeds the configured upload limit of {settings.upload_max_bytes:,} bytes.")
        return
    # This path is inside DATA_DIRECTORY and remains hidden from discovery until the
    # download is complete and atomically renamed.
    settings.data_directory.mkdir(parents=True, exist_ok=True)
    temporary_path = safe_upload_path(settings.data_directory, filename)
    final_path = temporary_path.with_suffix("")
    status_message = await message.reply_text(f"📦 فایل دریافت شد\n{filename}\n⏳ آماده‌سازی دانلود...")
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
        temporary_path.replace(final_path)
    except Exception:
        LOGGER.exception("Telegram download failed user_id=%s filename=%s", update.effective_user.id, filename)
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not remove incomplete upload path=%s", temporary_path)
        await status_message.edit_text("❌ دانلود ناموفق بود. فایل ناقص حذف شد و ایمپورتی شروع نشد.")
        return
    manager: UploadManager = context.application.bot_data["upload_manager"]
    position = await manager.enqueue(UploadJob(
        path=final_path,
        original_filename=filename,
        file_id=document.file_id,
        file_unique_id=document.file_unique_id,
        file_size=document.file_size,
        uploader_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        status_message_id=status_message.message_id,
    ))
    await status_message.edit_text(f"✅ دانلود کامل شد\n{filename}\n📥 در صف ایمپورت قرار گرفت؛ موقعیت: {position}")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if await deny_if_unauthorized(update, settings):
        return
    try:
        statistics = await asyncio.to_thread(run_stats, settings)
    except Exception:
        LOGGER.exception("Stats request failed for user id=%s", update.effective_user.id)
        await update.effective_message.reply_text("Statistics are temporarily unavailable.")
        return
    size_mib = float(statistics["database_bytes"]) / (1024 * 1024)
    await update.effective_message.reply_text(
        "<b>Database statistics</b>\n"
        f"Total records: {statistics['records']:,}\n"
        f"Imported files: {statistics['completed_files']:,}\n"
        f"In progress: {statistics['importing_files']:,}\n"
        f"Failed files: {statistics['failed_files']:,}\n"
        f"Database size: {size_mib:,.1f} MiB\n"
        f"Last import: {escape(str(statistics['last_import']))}\n"
        f"Searchable fields: 5 exact/prefix fields plus full-text name/row search\n"
        f"Search indexes ready: {'yes' if statistics['search_indexes_ready'] else 'no'}",
        parse_mode=ParseMode.HTML,
    )


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    callback = update.callback_query
    if callback is None:
        return
    if not authorized(update, settings):
        await callback.answer("Access denied.", show_alert=True)
        return
    try:
        _, token, page_value = (callback.data or "").split(":", 2)
        page = int(page_value)
        sessions: dict[str, QuerySession] = context.user_data.get(SESSION_KEY, {})
        session = sessions[token]
    except (KeyError, ValueError):
        await callback.answer("This result page has expired. Run the search again.", show_alert=True)
        return
    await callback.answer()
    try:
        result = await asyncio.to_thread(run_search, settings, session.query, page)
    except Exception:
        LOGGER.exception("Pagination search failed for user id=%s", update.effective_user.id)
        await callback.edit_message_text("The search is temporarily unavailable. Please run it again.")
        return
    visible_total = min(result.total_count, settings.max_results)
    if not result.rows or page * settings.results_per_page >= visible_total:
        await callback.answer("No more results.", show_alert=True)
        return
    await callback.edit_message_text(
        format_page(session.query, page, result, settings),
        parse_mode=ParseMode.HTML,
        reply_markup=pagination_keyboard(token, page, result.total_count, settings),
        disable_web_page_preview=True,
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
        finally:
            connection.close()
        settings.validate_for_bot()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}")
    global LOGGER
    LOGGER = configure_logging(settings.log_directory, "bot.log", settings.log_level)
    LOGGER.info("Bot starting; allowed user count=%d", len(settings.allowed_user_ids))

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
