"""Shared user-facing progress bars and stage-specific ETA estimation."""
from __future__ import annotations

import math
import time


_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def persian_digits(value: object) -> str:
    return str(value).translate(_PERSIAN_DIGITS)


def format_remaining_time(seconds: float) -> str:
    """Format an ETA compactly, in Persian, without false precision."""
    if not math.isfinite(seconds) or seconds < 0:
        return "در حال برآورد"
    if seconds < 0.5:
        return "تکمیل شد"
    total = max(1, int(round(seconds)))
    if total < 60:
        return f"حدود {persian_digits(total)} ثانیه"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        if secs < 5:
            return f"حدود {persian_digits(minutes)} دقیقه"
        return f"حدود {persian_digits(minutes)} دقیقه و {persian_digits(secs)} ثانیه"
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return f"حدود {persian_digits(hours)} ساعت"
    return f"حدود {persian_digits(hours)} ساعت و {persian_digits(minutes)} دقیقه"


class RemainingTimeEstimator:
    """Estimate time for one stage from its own observed throughput.

    ``completed`` and ``total`` must use the same unit (rows, bytes, or a
    stage-local progress scale). An unknown row count can therefore use the
    known source-file byte range instead of inventing a total row count.
    Each stage should own a separate estimator; rates are never carried from
    CSV ingestion into SQLite index construction.
    """

    def __init__(
        self,
        *,
        minimum_elapsed_seconds: float = 5.0,
        minimum_progress_fraction: float = 0.01,
        smoothing: float = 0.30,
    ) -> None:
        self.minimum_elapsed_seconds = max(0.0, minimum_elapsed_seconds)
        self.minimum_progress_fraction = max(0.0, min(minimum_progress_fraction, 1.0))
        self.smoothing = max(0.01, min(smoothing, 1.0))
        self.started_at: float | None = None
        self.last_at: float | None = None
        self.last_completed: float | None = None
        self.rate_per_second: float | None = None
        self.rate_samples = 0

    def estimate(
        self,
        completed: float,
        total: float | None,
        *,
        now: float | None = None,
        finished: bool = False,
    ) -> str:
        timestamp = time.monotonic() if now is None else float(now)
        value = max(0.0, float(completed))
        if self.started_at is None:
            self.started_at = timestamp
        if total is None or not math.isfinite(float(total)) or float(total) <= 0:
            self._remember(value, timestamp)
            return "برآورد ممکن نیست؛ حجم کل این مرحله مشخص نیست"
        maximum = float(total)
        value = min(value, maximum)
        if finished or value >= maximum:
            self._remember(value, timestamp)
            return "تکمیل شد"

        self._remember(value, timestamp)
        elapsed = max(0.0, timestamp - self.started_at)
        fraction = value / maximum
        if (
            self.rate_per_second is None
            or self.rate_samples < 2
            or elapsed < self.minimum_elapsed_seconds
            or fraction < self.minimum_progress_fraction
        ):
            return "در حال برآورد بر اساس سرعت همین مرحله"

        remaining = max(0.0, maximum - value) / self.rate_per_second
        return format_remaining_time(remaining)

    def _remember(self, completed: float, timestamp: float) -> None:
        if self.last_at is not None and self.last_completed is not None:
            elapsed = timestamp - self.last_at
            delta = completed - self.last_completed
            if elapsed > 0 and delta > 0:
                observed = delta / elapsed
                if math.isfinite(observed) and observed > 0:
                    if self.rate_per_second is None:
                        self.rate_per_second = observed
                    else:
                        alpha = self.smoothing
                        self.rate_per_second = alpha * observed + (1.0 - alpha) * self.rate_per_second
                    self.rate_samples += 1
        self.last_at = timestamp
        self.last_completed = completed


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
