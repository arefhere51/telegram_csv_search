"""Persistent search activity audit and UTF-8 report helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import time

from database import connect, utc_now


def _local_zone(settings):
    try:
        return ZoneInfo(getattr(settings, "bot_timezone", "Asia/Tehran"))
    except Exception:
        return timezone.utc


def start_event(settings, update, query: str, *, dataset_name: str = "همهٔ دیتاست‌ها") -> int | None:
    if not getattr(settings, "search_audit_enabled", True):
        return None
    database_path = getattr(settings, "database_path", None)
    if database_path is None:
        return None
    user = update.effective_user
    if user is None:
        return None
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    username = getattr(user, "username", None) or ""
    display = " ".join(x for x in (first, last) if x).strip() or username or str(user.id)
    stored_query = query if getattr(settings, "search_audit_store_query", True) else None
    connection = connect(database_path)
    try:
        cursor = connection.execute(
            """INSERT INTO search_activity
               (started_at, telegram_user_id, first_name, last_name, display_name,
                username, dataset_name, query_text, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running')""",
            (utc_now(), int(user.id), first, last, display, username, dataset_name, stored_query),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def finish_event(settings, event_id: int | None, status: str, result_count: int | None = None,
                 started_perf: float | None = None) -> None:
    if event_id is None or getattr(settings, "database_path", None) is None:
        return
    duration = int((time.perf_counter() - started_perf) * 1000) if started_perf is not None else None
    connection = connect(settings.database_path)
    try:
        connection.execute(
            "UPDATE search_activity SET completed_at=?, status=?, result_count=?, duration_ms=? WHERE event_id=?",
            (utc_now(), status, result_count, duration, int(event_id)),
        )
        connection.commit()
    finally:
        connection.close()


def _period_start(settings, period: str) -> str | None:
    now = datetime.now(_local_zone(settings))
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    if period == "7d":
        return (now - timedelta(days=7)).astimezone(timezone.utc).isoformat()
    return None


def dashboard_metrics(settings, queue=None) -> dict[str, int | str]:
    connection = connect(settings.database_path, readonly=True)
    try:
        start = _period_start(settings, "today")
        row = connection.execute(
            """SELECT COUNT(*) total,
               SUM(status IN ('success','no_results')) completed,
               SUM(status='no_results') no_results, SUM(status='failed') failed,
               SUM(status='cancelled') cancelled,
               COUNT(DISTINCT telegram_user_id) users_today
               FROM search_activity WHERE started_at >= ?""", (start,)
        ).fetchone()
        week = connection.execute(
            "SELECT COUNT(DISTINCT telegram_user_id) FROM search_activity WHERE started_at >= ?",
            (_period_start(settings, "7d"),),
        ).fetchone()[0]
        users = connection.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
        datasets = connection.execute("SELECT COUNT(*) FROM imported_files WHERE status='completed'").fetchone()[0]
        records = connection.execute("SELECT COALESCE(SUM(rows_imported),0) FROM imported_files WHERE status='completed'").fetchone()[0]
        mode = connection.execute("SELECT value FROM metadata WHERE key='access_mode'").fetchone()
        return {
            "total": int(row[0] or 0), "completed": int(row[1] or 0), "no_results": int(row[2] or 0),
            "failed": int(row[3] or 0), "cancelled": int(row[4] or 0), "users_today": int(row[5] or 0),
            "users_week": int(week or 0), "users": int(users), "datasets": int(datasets),
            "records": int(records), "queue": int(len(queue._queue) if queue is not None else 0),
            "mode": str(mode[0]) if mode else "private",
        }
    finally:
        connection.close()


def export_report(settings, destination: Path, period: str) -> bool:
    connection = connect(settings.database_path, readonly=True)
    try:
        start = _period_start(settings, period)
        query = "SELECT * FROM search_activity"
        params: tuple = ()
        if start:
            query += " WHERE started_at >= ?"
            params = (start,)
        query += " ORDER BY event_id ASC"
        rows = connection.execute(query, params)
        count = 0
        with destination.open("w", encoding="utf-8") as output:
            output.write("TELEGRAM CSV SEARCH BOT\nSEARCH ACTIVITY AUDIT REPORT\n" + "=" * 40 + "\n")
            output.write(f"Period: {period}\nQuery capture: {'enabled' if getattr(settings, 'search_audit_store_query', True) else 'disabled'}\n\n")
            for row in rows:
                count += 1
                output.write(f"EVENT #{int(row['event_id']):06d}\n")
                output.write(f"Timestamp: {row['started_at']}\nDisplay Name: {row['display_name']}\n")
                output.write(f"Telegram User ID: {row['telegram_user_id']}\nUsername: @{row['username']}\n")
                output.write(f"Dataset: {row['dataset_name']}\nQuery: {row['query_text'] or 'Not recorded'}\n")
                output.write(f"Status: {str(row['status']).upper()}\nResult Count: {row['result_count'] if row['result_count'] is not None else 'Unknown'}\n")
                output.write(f"Duration: {row['duration_ms'] if row['duration_ms'] is not None else 'Unknown'} ms\n{'-' * 40}\n")
            if not count:
                output.write("No search events found for this period.\n")
        return count > 0
    finally:
        connection.close()
