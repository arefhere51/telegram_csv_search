"""Shared user-facing progress bars."""
from __future__ import annotations


def progress_bar(percent: float, width: int = 18) -> str:
    value = max(0.0, min(float(percent), 100.0))
    filled = round(width * value / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def progress_text(
    title: str,
    stage: str,
    percent: float,
    *,
    detail: str | None = None,
) -> str:
    value = max(0.0, min(float(percent), 100.0))
    lines = [title, stage, f"{progress_bar(value)} {value:5.1f}%"]
    if detail:
        lines.append(detail)
    return "\n".join(lines)
