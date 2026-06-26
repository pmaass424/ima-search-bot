import os
import time
from pathlib import Path

from ..recency import path_recency_key, path_report_timestamp, report_day, target_report_day_from_env
from ..state import SourceItem, file_digest


class LocalFolderConnector:
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
    INCOMPLETE_SUFFIXES = {
        ".qkdownloading",
        ".crdownload",
        ".download",
        ".part",
        ".tmp",
    }

    def __init__(self, watch_dir: Path) -> None:
        self.watch_dir = watch_dir

    def list_items(self) -> list[SourceItem]:
        min_age_seconds = int(os.getenv("LOCAL_FILE_MIN_AGE_SECONDS", "120"))
        max_items = int(os.getenv("LOCAL_MAX_ITEMS", "200"))
        excluded_dirs = {
            item.strip()
            for item in os.getenv("LOCAL_EXCLUDE_DIRS", "").split(",")
            if item.strip()
        }
        paths = []
        for path in self.watch_dir.rglob("*"):
            if not path.is_file():
                continue
            if excluded_dirs and any(part in excluded_dirs for part in path.parts):
                continue
            if not self._is_ready_file(path, min_age_seconds):
                continue
            paths.append(path)

        target_day = target_report_day_from_env()
        if target_day:
            paths = [
                path
                for path in paths
                if report_day(path_report_timestamp(path)) == target_day
            ]
        elif os.getenv("LOCAL_LATEST_ONLY", "1") != "0":
            latest_day = _latest_report_day(paths)
            if latest_day:
                paths = [
                    path
                    for path in paths
                    if report_day(path_report_timestamp(path)) == latest_day
                ]

        paths.sort(key=path_recency_key, reverse=True)
        if max_items > 0:
            paths = paths[:max_items]

        items: list[SourceItem] = []
        for path in paths:
            items.append(
                SourceItem(
                    source_id=f"local:{path.resolve()}",
                    path=path,
                    title=path.name,
                    kind=path.suffix.lower().lstrip("."),
                    digest=file_digest(path),
                )
            )
        return items

    def _is_ready_file(self, path: Path, min_age_seconds: int) -> bool:
        suffixes = {suffix.lower() for suffix in path.suffixes}
        if suffixes & self.INCOMPLETE_SUFFIXES:
            return False
        if path.suffix.lower() not in self.SUPPORTED:
            return False
        if any(part.startswith(".") for part in path.parts):
            return False
        try:
            stat = path.stat()
        except FileNotFoundError:
            return False
        if stat.st_size <= 0:
            return False
        if min_age_seconds > 0 and time.time() - stat.st_mtime < min_age_seconds:
            return False
        return True


def _latest_report_day(paths: list[Path]) -> str:
    days = {
        day
        for path in paths
        for day in [report_day(path_report_timestamp(path))]
        if day is not None
    }
    return max(days) if days else ""
