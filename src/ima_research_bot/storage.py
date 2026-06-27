import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.config import Config


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    endpoint: str = ""
    prefix: str = "research"


@dataclass(frozen=True)
class R2Object:
    key: str
    size: int
    etag: str
    last_modified: Any


class R2Storage:
    def __init__(self, config: R2Config) -> None:
        self.config = config
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.bucket
            and self.config.access_key_id
            and self.config.secret_access_key
            and (self.config.endpoint or self.config.account_id)
        )

    @property
    def endpoint_url(self) -> str:
        if self.config.endpoint:
            return self.config.endpoint
        return f"https://{self.config.account_id}.r2.cloudflarestorage.com"

    def list_objects(self, prefix: str = "", limit: int = 1000) -> list[R2Object]:
        prefix = self._key(prefix)
        objects: list[R2Object] = []
        token: Optional[str] = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self.config.bucket,
                "Prefix": prefix,
                "MaxKeys": min(max(limit - len(objects), 1), 1000),
            }
            if token:
                kwargs["ContinuationToken"] = token
            payload = self._client.list_objects_v2(**kwargs)
            for row in payload.get("Contents") or []:
                objects.append(
                    R2Object(
                        key=str(row["Key"]),
                        size=int(row.get("Size") or 0),
                        etag=str(row.get("ETag") or "").strip('"'),
                        last_modified=row.get("LastModified"),
                    )
                )
                if len(objects) >= limit:
                    return objects
            if not payload.get("IsTruncated"):
                return objects
            token = payload.get("NextContinuationToken")
            if not token:
                return objects

    def upload_file(
        self,
        path: Path,
        key: str,
        *,
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        key = self._key(key)
        extra: dict[str, Any] = {"Metadata": metadata or {}}
        content_type = mimetypes.guess_type(path.name)[0]
        if content_type:
            extra["ContentType"] = content_type
        self._client.upload_file(str(path), self.config.bucket, key, ExtraArgs=extra)
        return key

    def download_file(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.config.bucket, key, str(destination))
        return destination

    def object_exists(self, key: str) -> bool:
        key = self._key(key)
        try:
            self._client.head_object(Bucket=self.config.bucket, Key=key)
            return True
        except Exception:
            return False

    def _key(self, key: str) -> str:
        key = key.strip("/")
        prefix = self.config.prefix.strip("/")
        if not prefix:
            return key
        if not key:
            return prefix
        if key == prefix or key.startswith(prefix + "/"):
            return key
        return f"{prefix}/{key}"
