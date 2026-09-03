"""S3-compatible object store, backed by aioboto3.

Speaks plain S3, so MinIO in development and real S3 (or any compatible
service) in production is a configuration change rather than a code path.

The client is opened once for the process rather than per call. Creating an
aiobotocore client involves loading service models and resolving endpoints,
which is slow enough that doing it per request is the classic throughput bug
in async S3 code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from wascat.core.config import get_settings
from wascat.storage.base import ObjectInfo, ObjectStore, PutResult


class S3ObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        public_base_url: str = "",
    ) -> None:
        self.bucket = bucket
        self._endpoint_url = endpoint_url
        self._public_base_url = public_base_url.rstrip("/")
        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._stack: AsyncExitStack | None = None
        self._client: Any = None

    @classmethod
    def from_settings(cls) -> S3ObjectStore:
        settings = get_settings()
        return cls(
            bucket=settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
            public_base_url=settings.public_asset_base_url,
        )

    async def __aenter__(self) -> S3ObjectStore:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._stack = AsyncExitStack()
        self._client = await self._stack.enter_async_context(
            self._session.client(  # type: ignore[call-overload]
                "s3",
                endpoint_url=self._endpoint_url,
                config=Config(
                    signature_version="s3v4",
                    # MinIO does not support virtual-host addressing without
                    # DNS wildcards, so paths it is.
                    s3={"addressing_style": "path"},
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        )

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("S3ObjectStore is not connected; call connect() first")
        return self._client

    async def ensure_bucket(self) -> None:
        try:
            await self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            await self.client.create_bucket(Bucket=self.bucket)

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str | None = None,
    ) -> PutResult:
        extra: dict[str, Any] = {"ContentType": content_type}
        if cache_control:
            extra["CacheControl"] = cache_control
        response = await self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)
        return PutResult(key=key, size=len(data), etag=response.get("ETag", "").strip('"'))

    async def get(self, key: str) -> bytes:
        response = await self.client.get_object(Bucket=self.bucket, Key=key)
        async with response["Body"] as stream:
            body: bytes = await stream.read()
        return body

    async def head(self, key: str) -> ObjectInfo | None:
        try:
            response = await self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return ObjectInfo(
            key=key,
            size=int(response["ContentLength"]),
            etag=response.get("ETag", "").strip('"'),
            content_type=response.get("ContentType"),
        )

    async def delete(self, key: str) -> None:
        await self.client.delete_object(Bucket=self.bucket, Key=key)

    async def list(self, prefix: str) -> AsyncIterator[ObjectInfo]:
        paginator = self.client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                yield ObjectInfo(
                    key=item["Key"],
                    size=int(item["Size"]),
                    etag=item.get("ETag", "").strip('"'),
                )

    async def presign_put(self, key: str, *, content_type: str, expires: int) -> str:
        url: str = await self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
        )
        return url

    async def presign_get(self, key: str, *, expires: int) -> str:
        url: str = await self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )
        return url

    def public_url(self, key: str) -> str:
        return f"{self._public_base_url}/{key}" if self._public_base_url else f"/{key}"
