"""The object store interface.

Frames, masks and derivatives live in object storage rather than the database.
Everything above this module speaks to the Protocol, so MinIO in development
and S3-behind-a-CDN in production differ by configuration alone - and the test
suite can use a directory on disk without needing either.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    key: str
    size: int
    etag: str | None = None
    content_type: str | None = None


@dataclass(frozen=True, slots=True)
class PutResult:
    key: str
    size: int
    etag: str | None = None


@runtime_checkable
class ObjectStore(Protocol):
    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str | None = None,
    ) -> PutResult: ...

    async def get(self, key: str) -> bytes: ...

    async def head(self, key: str) -> ObjectInfo | None: ...

    async def delete(self, key: str) -> None: ...

    def list(self, prefix: str) -> AsyncIterator[ObjectInfo]: ...

    async def presign_put(self, key: str, *, content_type: str, expires: int) -> str: ...

    async def presign_get(self, key: str, *, expires: int) -> str: ...

    def public_url(self, key: str) -> str: ...


# Frames within a published release never change - a correction becomes a new
# release with new keys - so they can be cached indefinitely.
IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
