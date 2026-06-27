import os
import re
from concurrent.futures import FIRST_COMPLETED, Future, wait
from base64 import b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .connectors.ima_tencent import ImaKnowledgeConnector
from .recency import path_report_timestamp, report_day, row_report_timestamp
from .state import StateStore, file_digest
from .storage import R2Storage


SUPPORTED_SUFFIXES = {
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


@dataclass(frozen=True)
class SyncResult:
    scanned: int
    uploaded: int
    skipped: int
    uploaded_keys: list[str]

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "uploaded": self.uploaded,
            "skipped": self.skipped,
            "uploaded_keys": self.uploaded_keys,
        }


class LocalToR2Sync:
    def __init__(self, root: Path, storage: R2Storage, state: StateStore) -> None:
        self.root = root.expanduser().resolve()
        self.storage = storage
        self.state = state

    def run(self, limit: int = 0) -> SyncResult:
        scanned = 0
        uploaded = 0
        skipped = 0
        uploaded_keys: list[str] = []
        workers = max(1, int(os.getenv("STORAGE_UPLOAD_WORKERS", "8")))
        pending: dict[Future, tuple[Path, str, str]] = {}

        def collect(done: set[Future]) -> None:
            nonlocal uploaded
            for future in done:
                path, digest, source_id = pending.pop(future)
                stored_key = future.result()
                self.state.record_stored_object(
                    storage_key=stored_key,
                    source_id=source_id,
                    title=path.name,
                    digest=digest,
                    size_bytes=path.stat().st_size,
                )
                uploaded += 1
                if uploaded == 1 or uploaded % 100 == 0:
                    print(f"uploaded {uploaded} files to R2; latest={stored_key}", flush=True)
                if len(uploaded_keys) < 50:
                    uploaded_keys.append(stored_key)

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as executor:
            limit_reached = False
            for path in self.root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                scanned += 1
                digest = file_digest(path)
                if self.state.has_stored_digest(digest):
                    skipped += 1
                    continue
                if limit > 0 and uploaded + len(pending) >= limit:
                    limit_reached = True
                    break
                key = self._key_for(path)
                source_id = f"local:{path.relative_to(self.root)}"
                future = executor.submit(
                    self.storage.upload_file,
                    path,
                    key,
                    metadata={
                        "source": "local-baseline",
                        "digest": digest,
                        "title": _metadata_value(path.name),
                    },
                )
                pending[future] = (path, digest, source_id)
                if len(pending) >= workers * 2:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    collect(done)

            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                collect(done)

            if limit_reached:
                print(f"upload limit reached at {limit} scheduled files", flush=True)
        return SyncResult(scanned=scanned, uploaded=uploaded, skipped=skipped, uploaded_keys=uploaded_keys)

    def _key_for(self, path: Path) -> str:
        relative = str(path.relative_to(self.root)).strip("/")
        return f"baseline/{relative}"


class ImaToR2Sync:
    def __init__(self, ima: ImaKnowledgeConnector, storage: R2Storage, state: StateStore) -> None:
        self.ima = ima
        self.storage = storage
        self.state = state

    def run(self, limit: Optional[int] = None) -> SyncResult:
        rows = self.ima.list_candidate_rows()
        max_uploads = int(os.getenv("IMA_R2_MAX_UPLOADS", os.getenv("IMA_MAX_DOWNLOADS_PER_RUN", "3")))
        if limit is not None:
            max_uploads = limit
        scanned = len(rows)
        uploaded = 0
        skipped = 0
        uploaded_keys: list[str] = []
        for row in rows:
            media_id = str(row.get("media_id") or "")
            if not media_id:
                skipped += 1
                continue
            source_id = f"ima:{media_id}"
            if self.state.has_stored_source(source_id):
                skipped += 1
                continue
            if max_uploads > 0 and uploaded >= max_uploads:
                break
            title = str(row.get("title") or row.get("name") or media_id)
            item = self.ima._download_media(media_id=media_id, title=title)
            if not item:
                skipped += 1
                continue
            try:
                if self.state.has_stored_digest(item.digest):
                    self.state.mark_processed(item)
                    skipped += 1
                    continue
                key = _ima_key(row, item.title)
                stored_key = self.storage.upload_file(
                    item.path,
                    key,
                    metadata={
                        "source": "ima",
                        "ima-media-id": media_id,
                        "digest": item.digest,
                        "title": _metadata_value(item.title),
                    },
                )
                self.state.record_stored_object(
                    storage_key=stored_key,
                    source_id=source_id,
                    title=item.title,
                    digest=item.digest,
                    size_bytes=item.path.stat().st_size,
                )
                self.state.mark_processed(item)
                uploaded += 1
                uploaded_keys.append(stored_key)
            finally:
                try:
                    item.path.unlink()
                except FileNotFoundError:
                    pass
        return SyncResult(scanned=scanned, uploaded=uploaded, skipped=skipped, uploaded_keys=uploaded_keys)


def _ima_key(row: dict, title: str) -> str:
    day = report_day(row_report_timestamp(row, text_fields=("title", "name", "_folder_path"))) or "undated"
    media_id = str(row.get("media_id") or "")[:32]
    return f"ima/{day}/{_safe_name(title, fallback=media_id)}"


def _safe_name(value: str, fallback: str = "file") -> str:
    path = Path(value)
    suffix = path.suffix
    stem = re.sub(r"[^A-Za-z0-9_.\-\u4e00-\u9fff]+", "_", path.stem).strip("._")
    stem = stem[:140] or fallback
    return f"{stem}{suffix}"


def _metadata_value(value: str) -> str:
    raw = value.encode("utf-8", errors="ignore")
    return b64encode(raw).decode("ascii")[:900]
