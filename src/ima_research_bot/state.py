import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class SourceItem:
    source_id: str
    path: Path
    title: str
    kind: str
    digest: str


@dataclass(frozen=True)
class StoredSummary:
    source_id: str
    title: str
    summary: str
    processed_at: str


@dataclass(frozen=True)
class StoredObject:
    storage_key: str
    source_id: str
    title: str
    digest: str
    size_bytes: int
    stored_at: str


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_items (
                    source_id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL,
                    processed_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS summaries (
                    source_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS openai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    usage_date TEXT NOT NULL,
                    category TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_usd REAL NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS stored_objects (
                    storage_key TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    stored_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_stored_objects_source ON stored_objects(source_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_stored_objects_digest ON stored_objects(digest)"
            )

    def is_processed(self, item: SourceItem) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT digest FROM processed_items WHERE source_id = ?",
                (item.source_id,),
            ).fetchone()
        return bool(row and row[0] == item.digest)

    def has_source(self, source_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_items WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        return bool(row)

    def mark_processed(self, item: SourceItem) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO processed_items(source_id, digest)
                VALUES(?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    digest = excluded.digest,
                    processed_at = CURRENT_TIMESTAMP
                """,
                (item.source_id, item.digest),
            )

    def record_summary(self, item: SourceItem, summary: str) -> None:
        processed_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO summaries(source_id, title, digest, summary, processed_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    title = excluded.title,
                    digest = excluded.digest,
                    summary = excluded.summary,
                    processed_at = excluded.processed_at
                """,
                (item.source_id, item.title, item.digest, summary, processed_at),
            )

    def recent_summaries(self, limit: int) -> list[StoredSummary]:
        if limit <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_id, title, summary, processed_at
                FROM summaries
                ORDER BY processed_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [StoredSummary(*row) for row in rows]

    def openai_spend_for_date(self, usage_date: str) -> float:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM openai_usage WHERE usage_date = ?",
                (usage_date,),
            ).fetchone()
        return float(row[0] or 0)

    def record_openai_usage(
        self,
        usage_date: str,
        category: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO openai_usage(
                    usage_date, category, model, input_tokens, output_tokens, cost_usd, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    usage_date,
                    category,
                    model,
                    int(input_tokens),
                    int(output_tokens),
                    float(cost_usd),
                    created_at,
                ),
            )

    def set_runtime_value(self, key: str, value: str) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runtime_kv(key, value, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, updated_at),
            )

    def runtime_value(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM runtime_kv WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row[0]) if row else None

    def record_stored_object(
        self,
        storage_key: str,
        source_id: str,
        title: str,
        digest: str,
        size_bytes: int,
    ) -> None:
        stored_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO stored_objects(storage_key, source_id, title, digest, size_bytes, stored_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(storage_key) DO UPDATE SET
                    source_id = excluded.source_id,
                    title = excluded.title,
                    digest = excluded.digest,
                    size_bytes = excluded.size_bytes,
                    stored_at = excluded.stored_at
                """,
                (storage_key, source_id, title, digest, int(size_bytes), stored_at),
            )

    def has_stored_source(self, source_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM stored_objects WHERE source_id = ? LIMIT 1",
                (source_id,),
            ).fetchone()
        return bool(row)

    def has_stored_digest(self, digest: str) -> bool:
        if not digest:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM stored_objects WHERE digest = ? LIMIT 1",
                (digest,),
            ).fetchone()
        return bool(row)

    def storage_stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM stored_objects"
            ).fetchone()
            latest = conn.execute(
                """
                SELECT storage_key, title, size_bytes, stored_at
                FROM stored_objects
                ORDER BY stored_at DESC
                LIMIT 20
                """
            ).fetchall()
        return {
            "stored_objects": int(total[0] or 0),
            "stored_bytes": int(total[1] or 0),
            "latest": [
                {
                    "storage_key": row[0],
                    "title": row[1],
                    "size_bytes": int(row[2] or 0),
                    "stored_at": row[3],
                }
                for row in latest
            ],
        }


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
