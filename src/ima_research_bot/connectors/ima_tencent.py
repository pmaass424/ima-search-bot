import hashlib
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

from ..recency import report_day, row_recency_key, row_recency_timestamp, row_report_timestamp, target_report_day_from_env
from ..state import SourceItem, StateStore, file_digest


@dataclass(frozen=True)
class ImaTencentConfig:
    base_url: str
    client_id: str
    api_key: str


class ImaApiError(RuntimeError):
    def __init__(self, code: Any, message: str) -> None:
        super().__init__(f"IMA API error {code}: {message}")
        self.code = code
        self.message = message

    @property
    def is_quota_error(self) -> bool:
        return str(self.code) == "220021"


class ImaTencentConnector:
    """IMA OpenAPI client.

    Official skill package uses:
    - Base URL: https://ima.qq.com
    - Headers: ima-openapi-clientid, ima-openapi-apikey
    - JSON POST endpoints under /openapi/note/v1 and /openapi/wiki/v1
    """

    def __init__(self, config: ImaTencentConfig) -> None:
        self.config = config

    @property
    def enabled(self) -> bool:
        return bool(self.config.base_url and self.config.client_id and self.config.api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "ima-openapi-clientid": self.config.client_id,
            "ima-openapi-apikey": self.config.api_key,
            "Content-Type": "application/json",
        }

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("IMA credentials are not configured")
        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"
        response = _request_with_retry("post", url, headers=self._headers(), json=body, timeout=60)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, None):
            raise ImaApiError(payload.get("code"), str(payload.get("msg") or ""))
        return payload.get("data", payload)

    def search_knowledge_bases(self, query: str = "", limit: int = 20) -> dict[str, Any]:
        return self.post(
            "/openapi/wiki/v1/search_knowledge_base",
            {"query": query, "cursor": "", "limit": limit},
        )

    def list_knowledge(
        self,
        knowledge_base_id: str,
        limit: int = 50,
        cursor: str = "",
        folder_id: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"knowledge_base_id": knowledge_base_id, "cursor": cursor, "limit": limit}
        if folder_id:
            body["folder_id"] = folder_id
        return self.post(
            "/openapi/wiki/v1/get_knowledge_list",
            body,
        )

    def search_knowledge(self, knowledge_base_id: str, query: str, cursor: str = "") -> dict[str, Any]:
        return self.post(
            "/openapi/wiki/v1/search_knowledge",
            {"knowledge_base_id": knowledge_base_id, "query": query, "cursor": cursor},
        )

    def get_media_info(self, media_id: str) -> dict[str, Any]:
        return self.post("/openapi/wiki/v1/get_media_info", {"media_id": media_id})

    def get_note_content(self, note_id: str) -> dict[str, Any]:
        return self.post(
            "/openapi/note/v1/get_doc_content",
            {"note_id": note_id, "target_content_format": 0},
        )

    def list_notes(self, limit: int = 20, cursor: str = "") -> dict[str, Any]:
        return self.post(
            "/openapi/note/v1/list_note",
            {"folder_id": "", "sort_type": 0, "cursor": cursor, "limit": limit},
        )

    def import_note(self, markdown: str, folder_id: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"content_format": 1, "content": markdown}
        if folder_id:
            body["folder_id"] = folder_id
        return self.post("/openapi/note/v1/import_doc", body)


class ImaKnowledgeConnector:
    SUPPORTED_MEDIA_TYPES = {1, 3, 4, 5, 7, 9, 11, 13}

    def __init__(
        self,
        client: ImaTencentConnector,
        knowledge_base_id: str,
        cache_dir: Path,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self.client = client
        self.knowledge_base_id = knowledge_base_id
        self.cache_dir = cache_dir
        self.state_store = state_store

    def list_items(self) -> list[SourceItem]:
        if not self.client.enabled or not self.knowledge_base_id:
            raise RuntimeError("Configure IMA_CLIENT_ID, IMA_API_KEY and IMA_KNOWLEDGE_BASE_ID in .env")

        rows = self.list_candidate_rows()
        items: list[SourceItem] = []
        max_downloads = int(os.getenv("IMA_MAX_DOWNLOADS_PER_RUN", "3"))
        for row in rows:
            media_id = row.get("media_id")
            if not media_id:
                continue
            source_id = f"ima:{media_id}"
            if self.state_store and self.state_store.has_source(source_id):
                continue
            if max_downloads > 0 and len(items) >= max_downloads:
                break
            title = str(row.get("title") or row.get("name") or media_id)
            try:
                item = self._download_media(media_id=media_id, title=title)
            except ImaApiError as exc:
                if exc.is_quota_error:
                    raise
                print(f"Skipping IMA media {media_id} ({title}): {exc}")
                continue
            except Exception as exc:
                print(f"Skipping IMA media {media_id} ({title}): {exc}")
                continue
            if item:
                items.append(item)
        return items

    def list_candidate_rows(self) -> list[dict[str, Any]]:
        folder_id = os.getenv("IMA_FOLDER_ID", "")
        folder_path: list[str] = []
        if os.getenv("IMA_LATEST_DATE_FOLDER", "0") == "1":
            latest_folder = self._latest_date_folder(
                folder_id=folder_id,
                max_pages=int(os.getenv("IMA_MAX_PAGES", "1")),
                max_depth=int(os.getenv("IMA_LATEST_DATE_FOLDER_DEPTH", "4")),
            )
            if latest_folder:
                folder_id, folder_name = latest_folder
                folder_path = [folder_name]
                print(f"IMA latest date folder: {folder_name} ({folder_id})")

        rows = self._list_all_knowledge(
            folder_id=folder_id,
            recursive=os.getenv("IMA_RECURSIVE", "0") == "1",
            max_pages=int(os.getenv("IMA_MAX_PAGES", "1")),
            folder_path=folder_path,
        )
        rows = self._filter_recent(rows)
        rows.sort(key=lambda row: row_recency_key(row, text_fields=("title", "name")), reverse=True)
        if os.getenv("IMA_BALANCE_FOLDERS", "1") != "0":
            rows = _interleave_by_folder(rows)
        max_items = int(os.getenv("IMA_MAX_ITEMS", "25"))
        if max_items > 0:
            rows = rows[:max_items]
        print(f"IMA candidate files: {len(rows)}")
        for row in rows[:10]:
            print(
                "IMA candidate:",
                row.get("title") or row.get("name") or row.get("media_id"),
                "folder=" + _folder_label(row),
            )
        return rows

    def _filter_recent(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        target_day = target_report_day_from_env(prefix="IMA")
        if target_day is not None:
            kept = [
                row
                for row in rows
                if report_day(row_report_timestamp(row, text_fields=("title", "name", "_folder_path"))) == target_day
            ]
            print(f"IMA target-day kept {len(kept)}/{len(rows)} candidates for report day {target_day}")
            return kept

        if os.getenv("IMA_LATEST_ONLY", "1") == "0":
            return rows

        latest_day = _latest_report_day(rows)
        if not latest_day:
            return rows
        kept = [
            row
            for row in rows
            if report_day(row_report_timestamp(row, text_fields=("title", "name", "_folder_path"))) == latest_day
        ]
        if kept:
            print(f"IMA latest-only kept {len(kept)}/{len(rows)} candidates for report day {latest_day}")
            return kept
        return rows

    def search_items(
        self,
        query: str,
        max_results: int = 8,
        max_downloads: int = 3,
    ) -> list[SourceItem]:
        if not self.client.enabled or not self.knowledge_base_id:
            raise RuntimeError("Configure IMA_CLIENT_ID, IMA_API_KEY and IMA_KNOWLEDGE_BASE_ID in .env")
        payload = self.client.search_knowledge(self.knowledge_base_id, query, cursor="")
        rows = payload.get("info_list") or []
        rows = rows[: max(1, max_results)]
        items: list[SourceItem] = []
        seen: set[str] = set()
        for row in rows:
            media_id = str(row.get("media_id") or "")
            if not media_id or media_id in seen:
                continue
            seen.add(media_id)
            title = str(row.get("title") or row.get("name") or media_id)
            item = self._download_media(media_id=media_id, title=title)
            if item:
                items.append(item)
            if max_downloads > 0 and len(items) >= max_downloads:
                break
        return items

    def _list_all_knowledge(
        self,
        folder_id: str,
        recursive: bool = False,
        max_pages: int = 2,
        folder_path: Optional[list[str]] = None,
    ) -> list[dict[str, Any]]:
        cursor = ""
        rows: list[dict[str, Any]] = []
        folders: list[tuple[str, str]] = []
        page_count = 0
        folder_path = folder_path or []

        while True:
            page_count += 1
            payload = self.client.list_knowledge(
                self.knowledge_base_id,
                cursor=cursor,
                limit=50,
                folder_id=folder_id,
            )
            entries = payload.get("knowledge_list") or payload.get("info_list") or []
            for entry in entries:
                media_type = entry.get("media_type")
                media_id = entry.get("media_id")
                folder_entry_id = entry.get("folder_id")
                if media_type == 99 and media_id:
                    folders.append((str(media_id), str(entry.get("title") or entry.get("name") or media_id)))
                elif media_id:
                    entry["_folder_path"] = "/".join(folder_path)
                    rows.append(entry)
                elif folder_entry_id:
                    folders.append((str(folder_entry_id), str(entry.get("title") or entry.get("name") or folder_entry_id)))

            for entry in payload.get("folder_list") or []:
                folder_entry_id = entry.get("folder_id")
                if folder_entry_id:
                    folders.append((str(folder_entry_id), str(entry.get("name") or entry.get("title") or folder_entry_id)))

            if payload.get("is_end", True):
                break
            if page_count >= max_pages:
                break
            cursor = payload.get("next_cursor") or ""
            if not cursor:
                break

        if recursive:
            seen_folders: set[str] = set()
            for child_folder_id, child_name in folders:
                if child_folder_id in seen_folders:
                    continue
                seen_folders.add(child_folder_id)
                rows.extend(
                    self._list_all_knowledge(
                        child_folder_id,
                        recursive=True,
                        max_pages=max_pages,
                        folder_path=folder_path + [child_name],
                    )
                )
        return rows

    def _latest_date_folder(
        self,
        folder_id: str,
        max_pages: int = 2,
        max_depth: int = 4,
    ) -> Optional[tuple[str, str]]:
        pending: list[tuple[str, list[str], int]] = [(folder_id, [], 0)]
        seen: set[str] = set()
        dated_folders: list[dict[str, Any]] = []

        while pending:
            current_folder_id, path, depth = pending.pop(0)
            if current_folder_id in seen:
                continue
            seen.add(current_folder_id)
            if depth > max_depth:
                continue

            children = self._list_child_folders(current_folder_id, max_pages=max_pages)
            for child_id, child_name in children:
                child_path = path + [child_name]
                label = "/".join(child_path)
                row = {"folder_id": child_id, "name": label}
                if row_recency_timestamp(row, text_fields=("name",)) > 0:
                    dated_folders.append(row)
                    continue
                pending.append((child_id, child_path, depth + 1))

        if not dated_folders:
            return None

        dated_folders.sort(key=lambda row: row_recency_key(row, text_fields=("name",)), reverse=True)
        latest = dated_folders[0]
        print(f"IMA dated folders scanned: {len(dated_folders)}")
        for row in dated_folders[:5]:
            print("IMA dated folder:", row["name"])
        return str(latest["folder_id"]), str(latest["name"])

    def _list_child_folders(self, folder_id: str, max_pages: int = 2) -> list[tuple[str, str]]:
        folders: list[tuple[str, str]] = []
        cursor = ""
        page_count = 0
        while True:
            page_count += 1
            payload = self.client.list_knowledge(
                self.knowledge_base_id,
                cursor=cursor,
                limit=50,
                folder_id=folder_id,
            )
            for entry in payload.get("knowledge_list") or payload.get("info_list") or []:
                media_id = entry.get("media_id")
                folder_entry_id = entry.get("folder_id")
                if entry.get("media_type") == 99 and media_id:
                    folders.append((str(media_id), str(entry.get("title") or entry.get("name") or media_id)))
                elif folder_entry_id:
                    folders.append((str(folder_entry_id), str(entry.get("title") or entry.get("name") or folder_entry_id)))

            for entry in payload.get("folder_list") or []:
                folder_entry_id = entry.get("folder_id")
                if folder_entry_id:
                    folders.append((str(folder_entry_id), str(entry.get("name") or entry.get("title") or folder_entry_id)))

            if payload.get("is_end", True):
                break
            if page_count >= max_pages:
                break
            cursor = payload.get("next_cursor") or ""
            if not cursor:
                break
        return folders

    def _download_media(self, media_id: str, title: str) -> Optional[SourceItem]:
        info = self.client.get_media_info(media_id)
        media_type = info.get("media_type")
        if media_type not in self.SUPPORTED_MEDIA_TYPES:
            return None

        if media_type == 11:
            note_id = (info.get("notebook_ext_info") or {}).get("notebook_id")
            if not note_id:
                return None
            content = self.client.get_note_content(str(note_id)).get("content", "")
            digest = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
            path = _temporary_path(title, media_id, ".txt")
            path.write_text(content, encoding="utf-8")
            return SourceItem(
                source_id=f"ima:{media_id}",
                path=path,
                title=title,
                kind="note",
                digest=digest,
            )

        url_info = info.get("url_info") or {}
        url = url_info.get("url")
        if not url:
            return None
        headers = url_info.get("headers") or {}
        response = _request_with_retry("get", url, headers=headers, timeout=180)
        response.raise_for_status()

        ext = _extension_for(title, response.headers.get("Content-Type", ""), media_type)
        path = _temporary_path(title, media_id, ext)
        path.write_bytes(response.content)
        return SourceItem(
            source_id=f"ima:{media_id}",
            path=path,
            title=title,
            kind=ext.lstrip(".") or "file",
            digest=file_digest(path),
        )


def _safe_file_name(title: str, media_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", title).strip("._")
    value = value[:90] or "ima_media"
    return f"{value}_{media_id[:16]}"


def _temporary_path(title: str, media_id: str, ext: str) -> Path:
    name = _safe_file_name(title, media_id)
    fd, raw_path = tempfile.mkstemp(prefix=f"{name}_", suffix=ext)
    os.close(fd)
    return Path(raw_path)


def _interleave_by_folder(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(_folder_bucket(row), []).append(row)

    buckets = sorted(
        grouped.values(),
        key=lambda bucket: row_recency_key(bucket[0], text_fields=("title", "name")),
        reverse=True,
    )
    balanced: list[dict[str, Any]] = []
    while buckets:
        next_buckets: list[list[dict[str, Any]]] = []
        for bucket in buckets:
            balanced.append(bucket.pop(0))
            if bucket:
                next_buckets.append(bucket)
        buckets = next_buckets
    return balanced


def _folder_bucket(row: dict[str, Any]) -> str:
    folder_path = str(row.get("_folder_path") or "").strip("/")
    if folder_path:
        return folder_path.split("/", 1)[0]
    return str(row.get("parent_folder_id") or "root")


def _folder_label(row: dict[str, Any]) -> str:
    return str(row.get("_folder_path") or row.get("parent_folder_id") or "root")


def _latest_report_day(rows: list[dict[str, Any]]) -> Optional[str]:
    days = {
        day
        for row in rows
        for day in [report_day(row_report_timestamp(row, text_fields=("title", "name", "_folder_path")))]
        if day is not None
    }
    return max(days) if days else None


def _extension_for(title: str, content_type: str, media_type: int) -> str:
    suffix = Path(title).suffix
    if suffix:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return guessed
    return {
        1: ".pdf",
        3: ".docx",
        4: ".pptx",
        5: ".xlsx",
        7: ".md",
        9: ".jpg",
        13: ".txt",
    }.get(media_type, ".bin")


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
