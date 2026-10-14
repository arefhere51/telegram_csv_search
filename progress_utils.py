"""Shared user-facing progress bars and operation correlation IDs."""
from __future__ import annotations

import secrets


def new_operation_id() -> str:
    """Return a short ID that can be matched between Telegram and server logs."""
    return secrets.token_hex(4).upper()


def progress_bar(percent: float, width: int = 18) -> str:
    value = max(0.0, min(float(percent), 100.0))
    filled = round(width * value / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def progress_text(
    title: str,
    stage: str,
    percent: float,
    *,
    operation_id: str | None = None,
    detail: str | None = None,
) -> str:
    value = max(0.0, min(float(percent), 100.0))
    lines = [title, stage, f"{progress_bar(value)} {value:5.1f}%"]
    if detail:
        lines.append(detail)
    if operation_id:
        lines.append(f"کد پیگیری: {operation_id}")
    return "\n".join(lines)
