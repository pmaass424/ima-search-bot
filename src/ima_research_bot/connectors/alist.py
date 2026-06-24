import os
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from ..recency import row_recency_key, row_recency_timestamp, row_report_timestamp
from ..state import SourceItem, StateStore, file_digest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AListConfig:
    base_url: str
    username: str = ""
    password: str = ""
    token: str = ""
    path: str = ""


class AListConnector:
    SUPPORTED = {
        ".pdf",
        ".txt",
        ".md",
        ".docx",
        ".pptx",
        ".xlsx",
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    }

    def __init__(
        self,
        config: AListConfig,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self._token = config.token

    @property
    def enabled(self) -> bool:
        return bool(self.config.base_url and self.config.path)

    def list_items(self) -> list[SourceItem]:
        if not self.enabled:
            raise RuntimeError("Configure ALIST_BASE_URL and ALIST_PATH")

        max_items = int(os.getenv("ALIST_MAX_ITEMS", "200"))
        max_downloads = int(os.getenv("ALIST_MAX_DOWNLOADS_PER_RUN", "3"))
        logger.info("listing AList path %s", self.config.path)
        rows = self._walk(self.config.path)
        logger.info("AList returned %s candidate remote files", len(rows))
        rows = self._filter_recent(rows)
        logger.info("AList kept %s candidate remote files after recency filter", len(rows))
        rows.sort(key=row_recency_key, reverse=True)
        if os.getenv("ALIST_BALANCE_SUBDIRS", "1") != "0":
            rows = _interleave_by_subdir(rows, self.config.path)
            logger.info("AList balanced candidates across first-level subdirectories")
        if max_items > 0:
            rows = rows[:max_items]
        for row in rows[:10]:
            logger.info(
                "AList recent candidate: %s modified=%s",
                row.get("path"),
                row.get("modified") or row.get("modified_at") or row.get("updated_at") or "",
            )

        items: list[SourceItem] = []
        for row in rows:
            path = str(row["path"])
            source_id = f"alist:{path}"
            if self.state_store and self.state_store.has_source(source_id):
                continue
            if max_downloads > 0 and len(items) >= max_downloads:
                break
            logger.info("downloading AList item %s", path)
            item = self._download(path, str(row.get("name") or Path(path).name))
            if item:
                items.append(item)
        return items

    def _filter_recent(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        target_day = _target_report_day()
        if target_day is not None:
            kept = [
                row
                for row in rows
                if _report_day(row_report_timestamp(row)) == target_day
            ]
            logger.info(
                "AList target-day kept %s/%s candidates for report day %s",
                len(kept),
                len(rows),
                target_day,
            )
            return kept

        latest_only = os.getenv("ALIST_LATEST_ONLY", "1") != "0"
        if latest_only:
            latest = _latest_report_day(rows)
            if latest is not None:
                kept = [
                    row
                    for row in rows
                    if _report_day(row_report_timestamp(row)) == latest
                ]
                if kept:
                    logger.info(
                        "AList latest-only kept %s/%s candidates for report day %s",
                        len(kept),
                        len(rows),
                        latest,
                    )
                    return kept
            logger.warning("AList latest-only filter matched nothing; falling back to configured date filter")

        min_timestamp = _min_recency_timestamp()
        if min_timestamp is None:
            return rows
        kept = [row for row in rows if row_recency_timestamp(row) >= min_timestamp]
        if kept:
            return kept
        logger.warning("AList recency filter matched nothing; keeping unfiltered rows to avoid missing all input")
        return rows

    def _headers(self) -> dict[str, str]:
        token = self._token or self._login()
        return {"Authorization": token} if token else {}

    def _login(self) -> str:
        url = f"{self.config.base_url.rstrip('/')}/api/auth/login"
        response = _request_with_retry(
            "post",
            url,
            json={"username": self.config.username, "password": self.config.password},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise RuntimeError(f"AList login failed: {payload.get('message')}")
        self._token = str(payload.get("data", {}).get("token") or "")
        return self._token

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"
        response = _request_with_retry(
            "post",
            url,
            headers=self._headers(),
            json=body,
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (200, 0):
            raise RuntimeError(f"AList API error {payload.get('code')}: {payload.get('message')}")
        return payload.get("data") or {}

    def _walk(self, root_path: str) -> list[dict[str, Any]]:
        recursive = os.getenv("ALIST_RECURSIVE", "1") != "0"
        max_pages = int(os.getenv("ALIST_MAX_PAGES", "20"))
        target_day = _target_report_day()
        min_timestamp = _min_recency_timestamp()
        pending = [root_path.rstrip("/") or "/"]
        files: list[dict[str, Any]] = []

        while pending:
            current = pending.pop(0)
            if _skip_directory(current, target_day, min_timestamp):
                logger.info("skipping old AList directory %s", current)
                continue
            logger.info("listing AList directory %s", current)
            for page in range(1, max_pages + 1):
                data = self._post(
                    "/api/fs/list",
                    {
                        "path": current,
                        "password": "",
                        "page": page,
                        "per_page": 100,
                        "refresh": False,
                    },
                )
                content = data.get("content") or []
                logger.info("AList directory %s page %s returned %s entries", current, page, len(content))
                rows = []
                for entry in content:
                    name = str(entry.get("name") or "")
                    full_path = f"{current.rstrip('/')}/{name}"
                    row = dict(entry)
                    row["path"] = full_path
                    rows.append(row)

                rows.sort(key=row_recency_key, reverse=True)
                for entry in rows:
                    name = str(entry.get("name") or "")
                    full_path = str(entry["path"])
                    if entry.get("is_dir"):
                        if recursive:
                            pending.append(full_path)
                        continue
                    if Path(name).suffix.lower() in self.SUPPORTED:
                        files.append(entry)
                if len(content) < 100:
                    break
        return files

    def _download(self, remote_path: str, title: str) -> Optional[SourceItem]:
        data = self._post("/api/fs/get", {"path": remote_path, "password": ""})
        url = data.get("raw_url")
        if not url:
            return None
        suffix = Path(title).suffix.lower() or ".bin"
        fd, raw_path = tempfile.mkstemp(prefix="alist_", suffix=suffix)
        os.close(fd)
        path = Path(raw_path)
        response = _request_with_retry("get", url, headers=self._headers(), timeout=180)
        response.raise_for_status()
        path.write_bytes(response.content)
        return SourceItem(
            source_id=f"alist:{remote_path}",
            path=path,
            title=title,
            kind=suffix.lstrip("."),
            digest=file_digest(path),
        )


def _min_recency_timestamp() -> Optional[float]:
    min_date = os.getenv("ALIST_MIN_REPORT_DATE", "").strip()
    if min_date:
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(min_date, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                pass
        logger.warning("Ignoring invalid ALIST_MIN_REPORT_DATE=%s", min_date)

    recent_days = int(os.getenv("ALIST_RECENT_DAYS", "7"))
    if recent_days <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=recent_days)).timestamp()


def _target_report_day() -> Optional[str]:
    target_date = os.getenv("ALIST_TARGET_REPORT_DATE", "").strip()
    if target_date:
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                return datetime.strptime(target_date, fmt).date().isoformat()
            except ValueError:
                pass
        logger.warning("Ignoring invalid ALIST_TARGET_REPORT_DATE=%s", target_date)

    days_ago = int(os.getenv("ALIST_TARGET_DAYS_AGO", "0"))
    if days_ago <= 0:
        return None
    tz = timezone(timedelta(hours=int(os.getenv("ALIST_TARGET_UTC_OFFSET_HOURS", "8"))))
    return (datetime.now(tz).date() - timedelta(days=days_ago)).isoformat()


def _latest_report_day(rows: list[dict[str, Any]]) -> Optional[str]:
    days = {
        day
        for row in rows
        for day in [_report_day(row_report_timestamp(row))]
        if day is not None
    }
    return max(days) if days else None


def _report_day(timestamp: float) -> Optional[str]:
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()


def _skip_directory(path: str, target_day: Optional[str], min_timestamp: Optional[float]) -> bool:
    timestamp = row_report_timestamp({"path": path, "name": path, "title": path})
    if timestamp > 0:
        day = _report_day(timestamp)
        if target_day:
            return day != target_day
        if min_timestamp is not None:
            return timestamp < min_timestamp
        return False

    month_bounds = _directory_month_bounds(path)
    if not month_bounds:
        return False
    month_start, month_end = month_bounds
    if target_day:
        target_dt = datetime.strptime(target_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return not (month_start <= target_dt <= month_end)
    if min_timestamp is not None:
        return month_end.timestamp() < min_timestamp
    return False


def _directory_month_bounds(path: str) -> Optional[tuple[datetime, datetime]]:
    year_match = re.search(r"(20\d{2})\s*年", path)
    month_match = re.search(r"(?:^|/)(0?[1-9]|1[0-2])\s*月(?:/|$)", path)
    if not year_match or not month_match:
        return None
    year = int(year_match.group(1))
    month = int(month_match.group(1))
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return start, next_month - timedelta(seconds=1)


def _interleave_by_subdir(rows: list[dict[str, Any]], root_path: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_first_subdir(row, root_path), []).append(row)

    interleaved: list[dict[str, Any]] = []
    while groups:
        empty_keys = []
        for key, group in groups.items():
            if group:
                interleaved.append(group.pop(0))
            if not group:
                empty_keys.append(key)
        for key in empty_keys:
            groups.pop(key, None)
    return interleaved


def _first_subdir(row: dict[str, Any], root_path: str) -> str:
    path = str(row.get("path") or "")
    root = root_path.rstrip("/") or "/"
    if path == root:
        rel = ""
    elif root == "/":
        rel = path.lstrip("/")
    elif path.startswith(f"{root}/"):
        rel = path[len(root) + 1 :]
    else:
        rel = path.lstrip("/")
    return rel.split("/", 1)[0] or "."


def _request_with_retry(method: str, url: str, attempts: int = 3, **kwargs) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            last_exc = requests.HTTPError(f"transient HTTP {response.status_code}", response=response)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
        if attempt < attempts:
            time.sleep(2 ** (attempt - 1))
    if last_exc:
        raise last_exc
    raise RuntimeError("request retry exhausted without response")
