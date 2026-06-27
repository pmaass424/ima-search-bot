import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional


DATE_PATTERNS = (
    re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(2[5-9]|3[0-9])(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)"),
    re.compile(r"(?<!\d)(20\d{2})[-._年](0?[1-9]|1[0-2])[-._月](0?[1-9]|[12]\d|3[01])(?:日)?(?!\d)"),
)
YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2})(?:年)?(?!\d)")
MONTH_CONTEXT_PATTERN = re.compile(r"(?:^|[/\\])\s*(0?[1-9]|1[0-2])月(?:[/\\]|$)")
MONTH_DAY_PATTERN = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[.-]([0-2]?\d|3[01])(?!\d)")


def path_recency_key(path: Path) -> tuple[float, float, str]:
    """Prefer report dates in filenames, then filesystem mtime."""
    mtime = path.stat().st_mtime
    return (_date_timestamp(path.name) or mtime, mtime, path.name)


def path_report_timestamp(path: Path) -> float:
    return _date_timestamp(str(path)) or _date_timestamp(path.name) or 0.0


def path_recency_timestamp(path: Path) -> float:
    return path_recency_key(path)[0]


def row_recency_key(
    row: dict[str, Any],
    *,
    text_fields: Iterable[str] = ("name", "title", "path"),
    time_fields: Iterable[str] = (
        "modified",
        "modified_at",
        "updated_at",
        "update_time",
        "created",
        "created_at",
        "create_time",
    ),
) -> tuple[float, float, str]:
    title_signal = max((_date_timestamp(str(row.get(field) or "")) or 0.0) for field in text_fields)
    metadata_signal = max((_timestamp(row.get(field)) or 0.0) for field in time_fields)
    label = " ".join(str(row.get(field) or "") for field in text_fields)
    primary = title_signal or metadata_signal
    return (primary, metadata_signal, label)


def row_recency_timestamp(row: dict[str, Any], *, text_fields: Iterable[str] = ("name", "title", "path")) -> float:
    return row_recency_key(row, text_fields=text_fields)[0]


def row_report_timestamp(row: dict[str, Any], *, text_fields: Iterable[str] = ("name", "title", "path")) -> float:
    return max((_date_timestamp(str(row.get(field) or "")) or 0.0) for field in text_fields)


def title_recency_key(title: str) -> tuple[float, str]:
    return (_date_timestamp(title) or 0.0, title)


def report_day(timestamp: float) -> Optional[str]:
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()


def target_report_day_from_env(prefix: str = "R2") -> Optional[str]:
    target_date = (
        os.getenv(f"{prefix}_TARGET_REPORT_DATE", "").strip()
        or os.getenv("TARGET_REPORT_DATE", "").strip()
    )
    if target_date:
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(target_date, fmt).date().isoformat()
            except ValueError:
                pass

    raw_days_ago = os.getenv(f"{prefix}_TARGET_DAYS_AGO", os.getenv("TARGET_DAYS_AGO", "")).strip()
    if raw_days_ago == "":
        return None
    days_ago = int(raw_days_ago)
    if days_ago < 0:
        return None
    tz = timezone(
        timedelta(
            hours=int(
                os.getenv(
                    f"{prefix}_TARGET_UTC_OFFSET_HOURS",
                    os.getenv("TARGET_UTC_OFFSET_HOURS", "8"),
                )
            )
        )
    )
    return (datetime.now(tz).date() - timedelta(days=days_ago)).isoformat()


def _date_timestamp(value: str) -> Optional[float]:
    best: Optional[float] = None
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(value):
            year, month, day = (int(part) for part in match.groups())
            if year < 100:
                year += 2000
            best = _max_timestamp(best, year, month, day)
    contextual = _contextual_month_day_timestamp(value)
    return max(item for item in (best, contextual) if item is not None) if best or contextual else None


def _contextual_month_day_timestamp(value: str) -> Optional[float]:
    matches = list(MONTH_DAY_PATTERN.finditer(value))
    if not matches:
        return None

    year_match = YEAR_PATTERN.search(value)
    year = int(year_match.group(1)) if year_match else datetime.now(timezone.utc).year
    month_context = _month_context(value)
    best: Optional[float] = None
    for match in matches:
        month = int(match.group(1))
        day = int(match.group(2))
        if month_context and month != month_context:
            continue
        best = _max_timestamp(best, year, month, day)
    return best


def _month_context(value: str) -> Optional[int]:
    matches = list(MONTH_CONTEXT_PATTERN.finditer(value))
    if not matches:
        return None
    return int(matches[-1].group(1))


def _max_timestamp(current: Optional[float], year: int, month: int, day: int) -> Optional[float]:
    try:
        candidate_dt = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return current
    if candidate_dt > datetime.now(timezone.utc) + timedelta(days=1):
        return current
    candidate = candidate_dt.timestamp()
    if current is None or candidate > current:
        return candidate
    return current


def _timestamp(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        return raw / 1000 if raw > 10_000_000_000 else raw

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _timestamp(int(text))

    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return _date_timestamp(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()
