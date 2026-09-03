"""Adding the frames the original catalogue left out.

The TypeScript pipeline sampled 100 unsegmented frames per sequence, because
keeping all of them would have added ~360 MB to a git repository. That
constraint belonged to the repository, not to the archive - object storage has
no such limit - so the remaining ~15,400 frames can now be ingested.

None of this needs the science. A frame with no mask has no cloud cover to
measure, so the backfill only hashes, probes and stores. That keeps the whole
operation clear of the greyscale-parity question, which applies solely to
re-measuring, and means the published measurements are untouched by design
rather than by care.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from anyio import to_thread
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.logging import get_logger
from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)
from wascat.storage import keys
from wascat.storage.base import IMMUTABLE_CACHE_CONTROL, ObjectStore
from wascat.storage.imaging import UnsupportedImageError, probe

log = get_logger(__name__)

#: Delivered filenames look like "1057_vid7_corner_mask.jpg".
DELIVERY = re.compile(r"^(?P<frame>\d+)_(?P<video>vid\d+)_")

#: Small objects, so the cost is round trips rather than bandwidth.
UPLOAD_CONCURRENCY = 24
INSERT_CHUNK = 500


@dataclass
class BackfillReport:
    scanned: int = 0
    already_present: int = 0
    added: int = 0
    uploaded_bytes: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.scanned:,} delivered frames scanned, "
            f"{self.already_present:,} already in the archive, "
            f"{self.added:,} added "
            f"({self.uploaded_bytes / 1024**2:.0f} MB uploaded)"
        )


@dataclass(frozen=True, slots=True)
class Delivered:
    path: Path
    video_id: str
    frame_index: int


def scan(source: Path) -> Iterator[Delivered]:
    """Every delivered source frame, identified from its filename."""
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg"}:
            continue
        match = DELIVERY.match(path.name)
        if match is None:
            continue
        yield Delivered(
            path=path,
            video_id=match["video"],
            frame_index=int(match["frame"]),
        )


def record_id(video_id: str, frame_index: int) -> str:
    """The identifier the original pipeline would have given this frame.

    Reproduced rather than invented, so a backfilled record is
    indistinguishable from one the pipeline produced - "WAS-V07-F1057".
    """
    return f"WAS-V{int(video_id[3:]):02d}-F{frame_index}"


async def backfill(
    session: AsyncSession,
    store: ObjectStore,
    *,
    source: Path,
    limit: int | None = None,
    dry_run: bool = False,
    into_version: str | None = None,
) -> BackfillReport:
    """Ingest every delivered frame the archive does not already hold.

    Idempotent: a frame already present is skipped, so an interrupted run can
    simply be repeated.

    Adding frames changes what a release contains, and a published release is
    immutable - so they go into a draft. Passing ``into_version`` creates or
    reuses a draft of that version per collection, which is the honest way to
    do this: the published 1.0 keeps meaning exactly what it meant when it was
    cited, and the larger archive becomes a new release that someone
    deliberately publishes.
    """
    report = BackfillReport()

    delivered = list(scan(source))
    report.scanned = len(delivered)
    if not delivered:
        return report

    existing = {row for row in (await session.execute(select(ImageRecord.id))).scalars().all()}

    collections = {
        collection.slug: collection
        for collection in (await session.execute(select(Collection))).scalars().all()
    }

    releases: dict[str, Release] = {}
    for slug, collection in collections.items():
        if into_version is not None:
            release = (
                (
                    await session.execute(
                        select(Release).where(
                            Release.collection_id == collection.id,
                            Release.version == into_version,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if release is None:
                release = Release(
                    collection_id=collection.id,
                    version=into_version,
                    status=ReleaseStatus.DRAFT,
                    meta={
                        "notes": (
                            "Created by the backfill to hold the delivered frames "
                            "the original sampled release left out."
                        )
                    },
                )
                if dry_run:
                    # Not persisted: it exists only so the preview can route
                    # frames and report a truthful count.
                    releases[slug] = release
                    continue
                session.add(release)
                await session.flush()
        else:
            release = (
                (
                    await session.execute(
                        select(Release)
                        .where(Release.collection_id == collection.id)
                        .order_by(Release.current.desc(), Release.version.desc())
                    )
                )
                .scalars()
                .first()
            )
        if release is not None:
            releases[slug] = release

    pending: list[Delivered] = []
    for item in delivered:
        identifier = record_id(item.video_id, item.frame_index)
        if identifier in existing:
            report.already_present += 1
            continue
        if item.video_id not in releases:
            report.skipped.append(f"{item.path.name}: no collection for {item.video_id}")
            continue
        pending.append(item)
        if limit is not None and len(pending) >= limit:
            break

    if dry_run or not pending:
        report.added = len(pending)
        return report

    # Published releases are immutable, so the target has to be a draft. Say
    # so before uploading 300 MB rather than after.
    blocked = {
        releases[item.video_id].version
        for item in pending
        if releases[item.video_id].status is not ReleaseStatus.DRAFT
    }
    if blocked:
        raise RuntimeError(
            "The target release of one or more sequences is published, and a "
            "published release is immutable - a citation of it has to keep "
            "resolving to the same data. Pass --into-version to put these "
            f"frames in a new draft instead. Affected: {', '.join(sorted(blocked))}."
        )

    semaphore = asyncio.Semaphore(UPLOAD_CONCURRENCY)
    rows: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    lock = asyncio.Lock()

    async def ingest(item: Delivered) -> None:
        async with semaphore:
            data = await to_thread.run_sync(item.path.read_bytes)
            try:
                probed = await to_thread.run_sync(probe, data)
            except UnsupportedImageError as exc:
                async with lock:
                    report.skipped.append(f"{item.path.name}: {exc}")
                return

            key = keys.frame_key(item.video_id, item.frame_index, "source")
            await store.put(
                key,
                data,
                content_type=probed.media_type,
                cache_control=IMMUTABLE_CACHE_CONTROL,
            )

            release = releases[item.video_id]
            identifier = record_id(item.video_id, item.frame_index)

            async with lock:
                rows.append(
                    {
                        "id": identifier,
                        "release_id": release.id,
                        "collection_id": release.collection_id,
                        "video_id": item.video_id,
                        "frame_index": item.frame_index,
                        # No mask, so no measurement. Absent, never zero.
                        "cloud_fraction": None,
                        "cloud_cover_oktas": None,
                        "width": probed.width,
                        "height": probed.height,
                        "condition_tags": [],
                        "provenance": {
                            "pipeline": "backfill",
                            "sourceDelivery": "corner_mask",
                            "segmented": False,
                            "note": (
                                "Delivered frame with no segmentation mask, so no "
                                "cloud cover has been measured."
                            ),
                        },
                    }
                )
                artifacts.append(
                    {
                        "image_id": identifier,
                        "type": "source",
                        "media_type": probed.media_type,
                        "object_key": key,
                        "checksum": probed.checksum,
                        "bytes": probed.bytes,
                        "width": probed.width,
                        "height": probed.height,
                    }
                )
                report.uploaded_bytes += probed.bytes

    await asyncio.gather(*(ingest(item) for item in pending))

    for start in range(0, len(rows), INSERT_CHUNK):
        await session.execute(insert(ImageRecord), rows[start : start + INSERT_CHUNK])
    for start in range(0, len(artifacts), INSERT_CHUNK):
        await session.execute(insert(Artifact), artifacts[start : start + INSERT_CHUNK])

    report.added = len(rows)
    return report


async def archive_totals(session: AsyncSession) -> dict[str, int]:
    """What the archive holds, for reporting before and after."""
    total = (await session.execute(select(func.count(ImageRecord.id)))).scalar_one()
    segmented = (
        await session.execute(
            select(func.count(ImageRecord.id)).where(ImageRecord.has_mask.is_(True))
        )
    ).scalar_one()
    artifacts = (await session.execute(select(func.count(Artifact.id)))).scalar_one()
    return {
        "records": total,
        "segmented": segmented,
        "unsegmented": total - segmented,
        "artifacts": artifacts,
    }
