"""Choosing and holding the object store.

Lives here rather than in ``core`` because ``core`` sits below ``storage`` in
the layering: the kernel must not know which backends exist.

One client for the process, opened on first use. Creating an aiobotocore
client loads service models and resolves endpoints, which is slow enough that
doing it per request is the classic throughput bug in async S3 code.
"""

from __future__ import annotations

from wascat.core.config import get_settings
from wascat.storage.base import ObjectStore
from wascat.storage.local import LocalObjectStore
from wascat.storage.s3 import S3ObjectStore

_store: ObjectStore | None = None


async def get_store() -> ObjectStore:
    global _store  # noqa: PLW0603
    if _store is not None:
        return _store

    settings = get_settings()
    if settings.storage_backend == "local":
        _store = LocalObjectStore(
            settings.local_storage_root,
            public_base_url=settings.public_asset_base_url,
        )
    else:
        s3 = S3ObjectStore.from_settings()
        await s3.connect()
        _store = s3
    return _store


async def close_store() -> None:
    """Called from the app lifespan on shutdown."""
    global _store  # noqa: PLW0603
    if isinstance(_store, S3ObjectStore):
        await _store.close()
    _store = None
