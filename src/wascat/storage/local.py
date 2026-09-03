"""Filesystem-backed object store.

Used by the test suite, so the great majority of tests need no Docker, and as
a fallback for a single-node deployment that has no object storage.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

from anyio import to_thread

from wascat.storage.base import ObjectInfo, ObjectStore, PutResult


class LocalObjectStore(ObjectStore):
    def __init__(self, root: str | Path, *, public_base_url: str = "") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._public_base_url = public_base_url.rstrip("/")

    def _path(self, key: str) -> Path:
        # Keys are built by storage.keys, never by user input, but a traversal
        # here would write outside the store, so it is checked anyway.
        path = (self.root / key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise ValueError(f"Key escapes the storage root: {key!r}")
        return path

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        cache_control: str | None = None,
    ) -> PutResult:
        path = self._path(key)
        await to_thread.run_sync(lambda: path.parent.mkdir(parents=True, exist_ok=True))
        await to_thread.run_sync(lambda: path.write_bytes(data))
        return PutResult(key=key, size=len(data), etag=hashlib.md5(data).hexdigest())  # noqa: S324

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        return await to_thread.run_sync(path.read_bytes)

    async def head(self, key: str) -> ObjectInfo | None:
        path = self._path(key)
        if not path.exists():
            return None
        return ObjectInfo(key=key, size=path.stat().st_size)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            await to_thread.run_sync(path.unlink)

    async def list(self, prefix: str) -> AsyncIterator[ObjectInfo]:
        base = self._path(prefix)
        if not base.exists():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file():
                yield ObjectInfo(
                    key=str(path.relative_to(self.root)).replace("\\", "/"),
                    size=path.stat().st_size,
                )

    async def presign_put(self, key: str, *, content_type: str, expires: int) -> str:
        raise NotImplementedError("The local store does not issue presigned URLs")

    async def presign_get(self, key: str, *, expires: int) -> str:
        return self.public_url(key)

    def public_url(self, key: str) -> str:
        return f"{self._public_base_url}/{key}" if self._public_base_url else f"/{key}"
