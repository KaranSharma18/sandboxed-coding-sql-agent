"""Object storage: S3-compatible (MinIO locally; S3 or GCS interoperability in production) or a folder.

Keys are always tenant-prefixed (`tenants/{tenant}/...`). The local backend exists for
single-machine development and tests; it signs URLs with an HMAC so the API can serve them.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from pathlib import Path
from typing import Any

from fixloop_core.errors import NotFound
from fixloop_core.hashing import sha256_bytes
from fixloop_core.models import ArtifactRef
from fixloop_core.ports import ObjectStore, task_prefix
from fixloop_core.settings import StorageSettings


def _safe_key(key: str) -> str:
    if not key or key.startswith("/") or ".." in key.split("/") or "\x00" in key:
        raise ValueError(f"invalid object key: {key!r}")
    return key


class LocalObjectStore:
    def __init__(self, root: Path, public_url: str = "http://localhost:8080", secret: str = "dev") -> None:  # noqa: S107 - tests only; services pass the configured secret
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_url = public_url.rstrip("/")
        self.secret = secret.encode()

    def _path(self, key: str) -> Path:
        return self.root / _safe_key(key)

    async def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> ArtifactRef:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        await asyncio.to_thread(tmp.write_bytes, data)
        await asyncio.to_thread(tmp.replace, path)
        return ArtifactRef(key=key, sha256=sha256_bytes(data), size=len(data), content_type=content_type)

    async def get_bytes(self, key: str, max_bytes: int | None = None) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise NotFound(f"object not found: {key}")
        data = await asyncio.to_thread(path.read_bytes)
        return data if max_bytes is None else data[:max_bytes]

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def size(self, key: str) -> int | None:
        path = self._path(key)
        return path.stat().st_size if path.exists() else None

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    async def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix.rstrip("/"))
        count = 0
        if base.is_dir():
            for item in sorted(base.rglob("*"), reverse=True):
                if item.is_file():
                    item.unlink()
                    count += 1
                else:
                    item.rmdir()
            base.rmdir()
        return count

    def sign(self, method: str, key: str, expires: int) -> str:
        message = f"{method}:{key}:{expires}".encode()
        return (
            base64.urlsafe_b64encode(hmac.new(self.secret, message, hashlib.sha256).digest())
            .decode()
            .rstrip("=")
        )

    def verify(self, method: str, key: str, expires: int, signature: str) -> bool:
        return expires >= int(time.time()) and hmac.compare_digest(self.sign(method, key, expires), signature)

    async def presign_get(self, key: str, ttl: int = 300) -> str:
        expires = int(time.time()) + ttl
        signature = self.sign("GET", key, expires)
        return f"{self.public_url}/v1/blobs/{_safe_key(key)}?expires={expires}&signature={signature}"

    async def presign_put(self, key: str, content_type: str, ttl: int = 3600) -> str:
        expires = int(time.time()) + ttl
        signature = self.sign("PUT", key, expires)
        return f"{self.public_url}/v1/blobs/{_safe_key(key)}?expires={expires}&signature={signature}"


class S3ObjectStore:
    """boto3 is synchronous; calls run in a thread so they never block the event loop."""

    def __init__(self, settings: StorageSettings) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.bucket
        kwargs: dict[str, Any] = {
            "region_name": settings.region,
            "config": Config(
                signature_version="s3v4",
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
                s3={"addressing_style": "path"},
            ),
        }
        if settings.access_key_id and settings.secret_access_key:
            kwargs["aws_access_key_id"] = settings.access_key_id.get_secret_value()
            kwargs["aws_secret_access_key"] = settings.secret_access_key.get_secret_value()
        self.client = boto3.client("s3", endpoint_url=settings.endpoint_url, **kwargs)
        # Presigned URLs must use the address clients can reach (MinIO is minio:9000 inside compose).
        public = settings.public_endpoint_url or settings.endpoint_url
        self.presigner = (
            boto3.client("s3", endpoint_url=public, **kwargs)
            if public != settings.endpoint_url
            else self.client
        )

    async def ensure_bucket(self) -> None:
        def work() -> None:
            try:
                self.client.head_bucket(Bucket=self.bucket)
            except Exception:
                self.client.create_bucket(Bucket=self.bucket)

        await asyncio.to_thread(work)

    async def put_bytes(
        self, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> ArtifactRef:
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=_safe_key(key),
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
        )
        return ArtifactRef(key=key, sha256=sha256_bytes(data), size=len(data), content_type=content_type)

    async def get_bytes(self, key: str, max_bytes: int | None = None) -> bytes:
        def work() -> bytes:
            try:
                params: dict[str, Any] = {"Bucket": self.bucket, "Key": _safe_key(key)}
                if max_bytes is not None:
                    params["Range"] = f"bytes=0-{max_bytes - 1}"
                return self.client.get_object(**params)["Body"].read()
            except self.client.exceptions.NoSuchKey as exc:
                raise NotFound(f"object not found: {key}") from exc

        return await asyncio.to_thread(work)

    async def size(self, key: str) -> int | None:
        def work() -> int | None:
            try:
                return int(self.client.head_object(Bucket=self.bucket, Key=_safe_key(key))["ContentLength"])
            except Exception:
                return None

        return await asyncio.to_thread(work)

    async def exists(self, key: str) -> bool:
        return await self.size(key) is not None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=_safe_key(key))

    async def delete_prefix(self, prefix: str) -> int:
        def work() -> int:
            count = 0
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=_safe_key(prefix)):
                keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                if keys:
                    self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
                    count += len(keys)
            return count

        return await asyncio.to_thread(work)

    async def presign_get(self, key: str, ttl: int = 300) -> str:
        return await asyncio.to_thread(
            self.presigner.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": _safe_key(key)},
            ExpiresIn=ttl,
        )

    async def presign_put(self, key: str, content_type: str, ttl: int = 3600) -> str:
        return await asyncio.to_thread(
            self.presigner.generate_presigned_url,
            "put_object",
            Params={"Bucket": self.bucket, "Key": _safe_key(key), "ContentType": content_type},
            ExpiresIn=ttl,
        )


def build_object_store(
    settings: StorageSettings,
    public_url: str = "http://localhost:8080",
    secret: str = "dev",  # noqa: S107
) -> LocalObjectStore | S3ObjectStore:
    if settings.backend == "local":
        return LocalObjectStore(settings.local_root, public_url, secret)
    return S3ObjectStore(settings)


__all__ = ["LocalObjectStore", "ObjectStore", "S3ObjectStore", "build_object_store", "task_prefix"]
