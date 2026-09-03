"""All catalogue SQL lives here.

Keeping queries in one module means the joins that the presenters depend on
(a record needs its collection slug and release version) are written once, and
the N+1 that would otherwise appear the first time someone renders a listing
never gets a chance to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from wascat.core.pagination import Cursor
from wascat.domains.catalog.filters import apply_cursor, apply_filters, apply_ordering
from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)
from wascat.domains.catalog.query import ImageQuery


@dataclass(frozen=True, slots=True)
class RecordRow:
    """An image record together with the identifiers the public shape needs."""

    record: ImageRecord
    collection_slug: str
    release_version: str


@dataclass(frozen=True, slots=True)
class Page:
    rows: list[RecordRow]
    total: int
    next_cursor: str | None


def _base_select() -> Select[Any]:
    return (
        select(ImageRecord, Collection.slug, Release.version)
        .join(Collection, Collection.id == ImageRecord.collection_id)
        .join(Release, Release.id == ImageRecord.release_id)
        .options(selectinload(ImageRecord.artifacts))
    )


async def list_images(session: AsyncSession, query: ImageQuery, cursor: Cursor) -> Page:
    """One page of records, plus the total the whole filter matches."""
    stmt = apply_filters(_base_select(), query)

    # The count runs against the same predicate but without the cursor, so
    # meta.total describes the whole result rather than what is left of it.
    count_stmt = apply_filters(
        select(func.count())
        .select_from(ImageRecord)
        .join(Collection, Collection.id == ImageRecord.collection_id)
        .join(Release, Release.id == ImageRecord.release_id),
        query,
    )
    total = (await session.execute(count_stmt)).scalar_one()

    if cursor.is_keyset and cursor.sort_key is not None and cursor.record_id is not None:
        stmt = apply_cursor(stmt, query, cursor.sort_key, cursor.record_id)
    elif cursor.offset:
        # Legacy offset cursors, still honoured for bookmarks issued by the
        # previous implementation.
        stmt = stmt.offset(cursor.offset)

    stmt = apply_ordering(stmt, query)
    # One extra row is the cheapest way to know whether another page exists.
    stmt = stmt.limit(query.limit + 1)

    result = (await session.execute(stmt)).all()
    has_more = len(result) > query.limit
    visible = result[: query.limit]

    rows = [
        RecordRow(record=row[0], collection_slug=row[1], release_version=row[2]) for row in visible
    ]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1].record
        next_cursor = Cursor(
            sort_key=last.sort_key,
            record_id=last.id,
            offset=cursor.offset + len(rows),
        ).encode()

    return Page(rows=rows, total=total, next_cursor=next_cursor)


async def get_image(session: AsyncSession, record_id: str) -> RecordRow | None:
    stmt = _base_select().where(ImageRecord.id == record_id, ImageRecord.retired_at.is_(None))
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return RecordRow(record=row[0], collection_slug=row[1], release_version=row[2])


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CollectionStats:
    video_ids: list[str]
    images: int
    artifacts: int
    with_source: int
    segmented: int
    min_frame: int | None
    max_frame: int | None
    total_bytes: int
    #: Mean measured cloud cover across the segmented frames, or None when the
    #: sequence has no measurement yet. Averaged in SQL because the page used
    #: to average the whole in-memory catalogue, which it no longer holds.
    mean_oktas: float | None
    #: Per-sequence mask registration: the scale each sequence's masks were
    #: delivered at relative to the frames they segment.
    mask_registration: list[tuple[str, float]]


async def collection_stats(session: AsyncSession, collection_id: Any) -> CollectionStats:
    """Counts the collection cards and detail pages display.

    ``artifacts`` counts only the frame and its mask. Derivatives are real
    rows but are not part of the published artifact set, and including them
    would change a number the site prints.
    """
    stmt = select(
        func.count(ImageRecord.id),
        func.count(ImageRecord.id).filter(ImageRecord.has_source),
        func.count(ImageRecord.id).filter(ImageRecord.has_mask),
        func.min(ImageRecord.frame_index),
        func.max(ImageRecord.frame_index),
        func.avg(ImageRecord.cloud_cover_oktas),
    ).where(
        ImageRecord.collection_id == collection_id,
        ImageRecord.retired_at.is_(None),
    )
    images, with_source, segmented, min_frame, max_frame, mean_oktas = (
        await session.execute(stmt)
    ).one()

    artifact_stmt = (
        select(func.count(Artifact.id), func.coalesce(func.sum(Artifact.bytes), 0))
        .join(ImageRecord, ImageRecord.id == Artifact.image_id)
        .where(
            ImageRecord.collection_id == collection_id,
            ImageRecord.retired_at.is_(None),
            Artifact.type.in_(("source", "mask")),
        )
    )
    artifacts, total_bytes = (await session.execute(artifact_stmt)).one()

    video_stmt = (
        select(ImageRecord.video_id)
        .where(
            ImageRecord.collection_id == collection_id,
            ImageRecord.retired_at.is_(None),
        )
        .group_by(ImageRecord.video_id, ImageRecord.video_number)
        .order_by(ImageRecord.video_number)
    )
    video_ids = list((await session.execute(video_stmt)).scalars().all())

    registration_stmt = (
        select(ImageRecord.video_id, func.max(ImageRecord.mask_scale))
        .where(
            ImageRecord.collection_id == collection_id,
            ImageRecord.retired_at.is_(None),
        )
        .group_by(ImageRecord.video_id, ImageRecord.video_number)
        .order_by(ImageRecord.video_number)
    )
    mask_registration = [
        (video_id, float(scale))
        for video_id, scale in (await session.execute(registration_stmt)).all()
    ]

    return CollectionStats(
        video_ids=video_ids,
        images=images or 0,
        artifacts=artifacts or 0,
        with_source=with_source or 0,
        segmented=segmented or 0,
        min_frame=min_frame,
        max_frame=max_frame,
        total_bytes=int(total_bytes or 0),
        mean_oktas=float(mean_oktas) if mean_oktas is not None else None,
        mask_registration=mask_registration,
    )


async def cover_record(session: AsyncSession, collection_id: Any) -> RecordRow | None:
    """The frame that represents a sequence.

    Prefers a fully paired frame so the sequence is shown at its best, then any
    frame with a source, then whatever exists. The ordering is explicit rather
    than left to the heap, or the cover would drift between deploys.
    """
    for condition in (
        (ImageRecord.has_source.is_(True), ImageRecord.has_mask.is_(True)),
        (ImageRecord.has_source.is_(True),),
        (),
    ):
        stmt = (
            _base_select()
            .where(
                ImageRecord.collection_id == collection_id,
                ImageRecord.retired_at.is_(None),
                *condition,
            )
            .order_by(ImageRecord.video_number, ImageRecord.frame_index)
            .limit(1)
        )
        row = (await session.execute(stmt)).first()
        if row is not None:
            return RecordRow(record=row[0], collection_slug=row[1], release_version=row[2])
    return None


async def list_collections(session: AsyncSession) -> list[Collection]:
    stmt = (
        select(Collection)
        .options(selectinload(Collection.releases))
        .order_by(Collection.position, Collection.slug)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_collection(session: AsyncSession, slug: str) -> Collection | None:
    stmt = (
        select(Collection).options(selectinload(Collection.releases)).where(Collection.slug == slug)
    )
    return (await session.execute(stmt)).scalars().first()


async def release_image_counts(
    session: AsyncSession, collection_id: Any
) -> dict[Any, tuple[int, int]]:
    """Image count and total bytes per release, for the release table."""
    stmt = (
        select(
            ImageRecord.release_id,
            func.count(func.distinct(ImageRecord.id)),
            func.coalesce(func.sum(Artifact.bytes), 0),
        )
        .outerjoin(
            Artifact,
            (Artifact.image_id == ImageRecord.id) & Artifact.type.in_(("source", "mask")),
        )
        .where(
            ImageRecord.collection_id == collection_id,
            ImageRecord.retired_at.is_(None),
        )
        .group_by(ImageRecord.release_id)
    )
    return {
        release_id: (count, int(total or 0))
        for release_id, count, total in (await session.execute(stmt)).all()
    }


async def current_release(session: AsyncSession, collection_id: Any) -> Release | None:
    stmt = select(Release).where(
        Release.collection_id == collection_id,
        Release.current.is_(True),
    )
    return (await session.execute(stmt)).scalars().first()


async def published_releases(session: AsyncSession, collection_id: Any) -> list[Release]:
    """Releases the public may see. Drafts are dashboard-only."""
    stmt = (
        select(Release)
        .where(
            Release.collection_id == collection_id,
            Release.status != ReleaseStatus.DRAFT,
        )
        .order_by(Release.version)
    )
    return list((await session.execute(stmt)).scalars().all())
