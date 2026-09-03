"""Accepting an image into the archive.

Uploads are taken server-side rather than presigned straight to object
storage. That is a deliberate trade: a presigned PUT is one fewer hop, but it
means trusting the client's claim about what it uploaded, and this archive
records a checksum and dimensions against every artifact. The server has to
see the bytes to know them. At ~25 KB a frame the extra hop costs nothing.

The order of operations matters. Everything that can be checked is checked
before anything is written, and object storage is written before the database
row that points at it - a stored object with no row is a tidy-up job, whereas a
row pointing at nothing is a broken record in a published archive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from anyio import to_thread
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.config import get_settings
from wascat.core.errors import (
    ConflictError,
    PayloadTooLargeError,
    UnprocessableUploadError,
)
from wascat.core.logging import get_logger
from wascat.domains.catalog.admin_service import assert_writable
from wascat.domains.catalog.models import Artifact, ImageRecord, Release
from wascat.storage import keys
from wascat.storage.base import IMMUTABLE_CACHE_CONTROL, ObjectStore
from wascat.storage.imaging import (
    DERIVATIVES,
    UnsupportedImageError,
    derive,
    probe,
)

log = get_logger(__name__)

PrimaryType = Literal["source", "mask"]


@dataclass(frozen=True, slots=True)
class UploadResult:
    record_id: str
    artifact_type: str
    object_key: str
    checksum: str
    width: int
    height: int
    bytes: int
    replaced: bool
    derivatives: list[str]


async def replace_artifact(
    session: AsyncSession,
    store: ObjectStore,
    *,
    record: ImageRecord,
    artifact_type: PrimaryType,
    data: bytes,
) -> UploadResult:
    """Attach or replace a record's source frame or mask.

    Refuses, in this order:
      * an upload into a published release, because that release is immutable;
      * anything that is not an image the archive accepts;
      * an image whose dimensions disagree with the record's.

    That last one is invariant 5. A mask that does not line up pixel-for-pixel
    with the frame it segments would make the measured cloud cover meaningless,
    so the check is not a formality.
    """
    settings = get_settings()

    if len(data) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"That file is {len(data) / 1024**2:.1f} MB. The limit is "
            f"{settings.max_upload_bytes / 1024**2:.0f} MB."
        )

    release = await session.get(Release, record.release_id)
    if release is not None:
        assert_writable(release)

    # Decoding and hashing are CPU-bound; on the event loop a large image
    # would stall every other request while it worked.
    try:
        probed = await to_thread.run_sync(probe, data)
    except UnsupportedImageError as exc:
        raise UnprocessableUploadError(str(exc)) from exc

    if (probed.width, probed.height) != (record.width, record.height):
        raise UnprocessableUploadError(
            f"That image is {probed.width}x{probed.height}, but record "
            f"{record.id} is {record.width}x{record.height}. A mask has to line "
            "up with the frame it segments, or the measured cloud cover means "
            "nothing."
        )

    existing = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.image_id == record.id, Artifact.type == artifact_type
                )
            )
        )
        .scalars()
        .first()
    )

    if existing is not None and existing.checksum == probed.checksum:
        # Byte-identical to what is already there. Saying so is more useful
        # than reporting a successful no-op.
        raise ConflictError(
            f"That is already the {artifact_type} for {record.id} - the file is "
            "byte-for-byte identical."
        )

    object_key = keys.frame_key(record.video_id, record.frame_index, artifact_type)

    # Object storage first. A stored object with no row is rubbish to collect;
    # a row pointing at a missing object is a broken record.
    await store.put(
        object_key,
        data,
        content_type=probed.media_type,
        cache_control=IMMUTABLE_CACHE_CONTROL,
    )

    if existing is None:
        artifact = Artifact(
            image_id=record.id,
            type=artifact_type,
            media_type=probed.media_type,
            object_key=object_key,
            checksum=probed.checksum,
            bytes=probed.bytes,
            width=probed.width,
            height=probed.height,
        )
        session.add(artifact)
    else:
        existing.media_type = probed.media_type
        existing.object_key = object_key
        existing.checksum = probed.checksum
        existing.bytes = probed.bytes
        existing.width = probed.width
        existing.height = probed.height

    generated = await _write_derivatives(store, record, artifact_type, data)

    # Replacing a mask changes what was measured, and the old measurement no
    # longer describes the file. Clearing it is the honest move: the record
    # reads as unsegmented until the pipeline measures the new mask, rather
    # than carrying a number that came from an image no longer present.
    if artifact_type == "mask" and existing is not None:
        record.cloud_fraction = None
        record.cloud_cover_oktas = None
        record.provenance = {
            **(record.provenance or {}),
            "maskReplaced": True,
            "measurementCleared": (
                "The mask was replaced, so the previous cloud cover no longer "
                "describes the stored file. Re-run the pipeline to measure it."
            ),
        }

    await session.flush()

    return UploadResult(
        record_id=record.id,
        artifact_type=artifact_type,
        object_key=object_key,
        checksum=probed.checksum,
        width=probed.width,
        height=probed.height,
        bytes=probed.bytes,
        replaced=existing is not None,
        derivatives=generated,
    )


async def _write_derivatives(
    store: ObjectStore,
    record: ImageRecord,
    artifact_type: str,
    data: bytes,
) -> list[str]:
    """Render and store the dashboard's smaller versions.

    These are not artifact rows. They are additive files the grids read, and
    counting them would change `collection.artifacts` - a number the public
    site prints - from 3,438 to something meaningless.

    A failure here is logged rather than raised: the frame itself is stored and
    correct, and losing a thumbnail should not fail the upload.
    """
    written: list[str] = []
    try:
        derivatives = await to_thread.run_sync(derive, data, DERIVATIVES)
    except Exception as exc:
        log.warning(
            "derivative.render_failed",
            record=record.id,
            artifact=artifact_type,
            error=str(exc),
        )
        return written

    for derivative in derivatives:
        key = keys.derivative_key(
            record.video_id,
            record.frame_index,
            f"{artifact_type}-{derivative.kind}",
            derivative.width,
        )
        try:
            await store.put(
                key,
                derivative.data,
                content_type=derivative.media_type,
                cache_control=IMMUTABLE_CACHE_CONTROL,
            )
            written.append(key)
        except Exception as exc:
            # The frame itself is stored and correct, so a missing thumbnail is
            # a degraded grid rather than a failed upload. Still worth knowing
            # about, so it is logged rather than dropped on the floor.
            log.warning("derivative.upload_failed", record=record.id, key=key, error=str(exc))

    return written


async def delete_artifact(
    session: AsyncSession,
    store: ObjectStore,
    *,
    record: ImageRecord,
    artifact_type: PrimaryType,
) -> None:
    """Remove a record's source frame or mask.

    The deferred constraint refuses a record left holding neither, so removing
    the only artifact fails at commit - which is the correct outcome, but the
    check is done here too so the message explains it.
    """
    release = await session.get(Release, record.release_id)
    if release is not None:
        assert_writable(release)

    # Queried rather than read off record.artifacts: that relationship is only
    # loaded when the record came from a query that asked for it, and a caller
    # who built the record another way would otherwise trip a lazy load in an
    # async context. Asking the database directly works for every caller.
    present = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.image_id == record.id,
                    Artifact.type.in_(("source", "mask")),
                )
            )
        )
        .scalars()
        .all()
    )

    if not [artifact for artifact in present if artifact.type != artifact_type]:
        raise ConflictError(
            f"{record.id} would then hold neither a frame nor a mask. Retire the "
            "record instead - a record with no imagery is not a record."
        )

    target = next((artifact for artifact in present if artifact.type == artifact_type), None)
    if target is None:
        return

    await session.delete(target)

    # Removing the mask removes the measurement's subject.
    if artifact_type == "mask":
        record.cloud_fraction = None
        record.cloud_cover_oktas = None

    await session.flush()

    # The object is left in storage on purpose. A published release may still
    # reference the same key, and an orphan file costs pennies where a
    # dangling reference costs a broken archive.
