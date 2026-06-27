import os
import re
from pathlib import Path

from ..recency import report_day, row_recency_key, row_report_timestamp, target_report_day_from_env
from ..state import SourceItem, StateStore, file_digest
from ..storage import R2Object, R2Storage


class R2Connector:
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

    def __init__(self, storage: R2Storage, cache_dir: Path, state_store: StateStore) -> None:
        self.storage = storage
        self.cache_dir = cache_dir
        self.state_store = state_store

    def list_items(self) -> list[SourceItem]:
        max_items = int(os.getenv("R2_MAX_ITEMS", "200"))
        max_downloads = int(os.getenv("R2_MAX_DOWNLOADS_PER_RUN", "3"))
        objects = [
            obj
            for obj in self.storage.list_objects(limit=max_items * 5 if max_items > 0 else 1000)
            if Path(obj.key).suffix.lower() in self.SUPPORTED and obj.size > 0
        ]
        objects = self._filter_recent(objects)
        objects.sort(key=lambda obj: row_recency_key(_row_for(obj)), reverse=True)
        if max_items > 0:
            objects = objects[:max_items]

        items: list[SourceItem] = []
        for obj in objects:
            source_id = f"r2:{obj.key}"
            if self.state_store.has_source(source_id):
                continue
            if max_downloads > 0 and len(items) >= max_downloads:
                break
            local_path = self.cache_dir / _safe_cache_name(obj.key)
            self.storage.download_file(obj.key, local_path)
            items.append(
                SourceItem(
                    source_id=source_id,
                    path=local_path,
                    title=Path(obj.key).name,
                    kind=Path(obj.key).suffix.lower().lstrip(".") or "file",
                    digest=file_digest(local_path),
                )
            )
        return items

    def _filter_recent(self, objects: list[R2Object]) -> list[R2Object]:
        target_day = target_report_day_from_env(prefix="R2")
        if target_day:
            return [
                obj
                for obj in objects
                if report_day(row_report_timestamp(_row_for(obj))) == target_day
            ]
        if os.getenv("R2_LATEST_ONLY", "1") == "0":
            return objects
        latest_day = _latest_report_day(objects)
        if not latest_day:
            return objects
        kept = [
            obj
            for obj in objects
            if report_day(row_report_timestamp(_row_for(obj))) == latest_day
        ]
        return kept or objects


def _row_for(obj: R2Object) -> dict:
    return {
        "name": Path(obj.key).name,
        "title": Path(obj.key).name,
        "path": obj.key,
        "modified": obj.last_modified,
    }


def _latest_report_day(objects: list[R2Object]) -> str:
    days = {
        day
        for obj in objects
        for day in [report_day(row_report_timestamp(_row_for(obj)))]
        if day is not None
    }
    return max(days) if days else ""


def _safe_cache_name(key: str) -> str:
    suffix = Path(key).suffix
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("._")[:140] or "r2_object"
    if suffix and not stem.endswith(suffix):
        return f"{stem}{suffix}"
    return stem
