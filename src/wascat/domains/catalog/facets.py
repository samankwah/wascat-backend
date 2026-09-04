"""Facet counts for the Explore filters.

The shape matters as much as the numbers. Vocabularies that the API validates
as a closed set - oktas, seasons, times of day, segmentation, artifact types -
always emit every member, including the ones at zero, so the UI can render a
stable list of filters rather than one that appears and disappears as the
archive changes. ``locations`` is the exception: it is open-ended, so it lists
only the values actually present, which today is none.

A plain GROUP BY would drop every zero and quietly change that. See risk R11.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.jsformat import okta_label
from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)
from wascat.domains.catalog.presenters import sequence_label
from wascat.domains.catalog.query import ARTIFACT_TYPES, SEASONS, TIMES_OF_DAY

Facet = dict[str, Any]


def _published() -> Any:
    """Records the public archive contains.

    A draft release is working state. Counting it here would make the facets
    disagree with the listings, and would announce a release before anyone
    published it.
    """
    return ImageRecord.release_id.in_(
        select(Release.id).where(Release.status != ReleaseStatus.DRAFT)
    )


async def _counts_by(session: AsyncSession, column: Any) -> dict[Any, int]:
    stmt = (
        select(column, func.count(ImageRecord.id))
        .where(ImageRecord.retired_at.is_(None), _published())
        .group_by(column)
    )
    return {key: count for key, count in (await session.execute(stmt)).all()}


async def build_facets(session: AsyncSession) -> dict[str, list[Facet]]:
    # -- collections ------------------------------------------------------
    collection_stmt = (
        select(
            Collection.position,
            Collection.slug,
            Collection.location_name,
            ImageRecord.sequence_id,
            func.count(ImageRecord.id),
        )
        .join(ImageRecord, ImageRecord.collection_id == Collection.id)
        .where(ImageRecord.retired_at.is_(None), _published())
        .group_by(
            Collection.position, Collection.slug, Collection.location_name, ImageRecord.sequence_id
        )
        .order_by(Collection.position, Collection.slug)
    )
    per_collection: dict[str, dict[str, Any]] = {}
    for _, slug, location_name, sequence_id, count in (
        await session.execute(collection_stmt)
    ).all():
        entry = per_collection.setdefault(
            slug, {"label": location_name, "sequences": [], "count": 0}
        )
        entry["sequences"].append(sequence_label(sequence_id))
        entry["count"] += count

    collections = [
        {
            "value": slug,
            # shortTitle: the site name once supplied, the sequence list until then.
            "label": entry["label"] or f"Sequence {', '.join(sorted(set(entry['sequences'])))}",
            "count": entry["count"],
        }
        for slug, entry in per_collection.items()
    ]

    # -- sequences ---------------------------------------------------------
    # Ordered by the identifier itself: it is zero-padded and fixed width, so
    # seq-010 follows seq-009 without a derived integer to sort on. The label
    # is what a reader sees; the value is what the filter sends back.
    sequence_stmt = (
        select(ImageRecord.sequence_id, func.count(ImageRecord.id))
        .where(ImageRecord.retired_at.is_(None), _published())
        .group_by(ImageRecord.sequence_id)
        .order_by(ImageRecord.sequence_id)
    )
    sequences = [
        {"value": sequence_id, "label": f"Sequence {sequence_label(sequence_id)}", "count": count}
        for sequence_id, count in (await session.execute(sequence_stmt)).all()
    ]

    # -- cloud cover ------------------------------------------------------
    # Measured records only. An unsegmented frame has no cover to bucket, and
    # counting it as 0 oktas would invent a clear sky.
    okta_counts = await _counts_by(session, ImageRecord.cloud_cover_oktas)
    cloud_cover = [
        {"value": okta, "label": okta_label(okta), "count": okta_counts.get(okta, 0)}
        for okta in range(9)
    ]

    # -- segmentation -----------------------------------------------------
    mask_counts = await _counts_by(session, ImageRecord.has_mask)
    segmentation = [
        {
            "value": "segmented",
            "label": "Has a cloud mask",
            "count": mask_counts.get(True, 0),
        },
        {
            "value": "unsegmented",
            "label": "Not yet segmented",
            "count": mask_counts.get(False, 0),
        },
    ]

    # -- artifacts: records carrying at least one of each type ------------
    artifact_stmt = (
        select(Artifact.type, func.count(func.distinct(Artifact.image_id)))
        .join(ImageRecord, ImageRecord.id == Artifact.image_id)
        .where(ImageRecord.retired_at.is_(None), _published())
        .group_by(Artifact.type)
    )
    artifact_counts: dict[str, int] = {
        name: count for name, count in (await session.execute(artifact_stmt)).all()
    }
    artifacts = [
        {"value": value, "count": artifact_counts.get(value, 0)} for value in ARTIFACT_TYPES
    ]

    # -- vocabularies -----------------------------------------------------
    season_counts = await _counts_by(session, ImageRecord.season_label)
    seasons = [{"value": value, "count": season_counts.get(value, 0)} for value in SEASONS]

    time_counts = await _counts_by(session, ImageRecord.time_of_day_label)
    times_of_day = [{"value": value, "count": time_counts.get(value, 0)} for value in TIMES_OF_DAY]

    # Open-ended, so only what is present. Empty until the capture team
    # supplies per-sequence provenance.
    location_counts = await _counts_by(session, ImageRecord.location_label)
    locations = [
        {"value": value, "count": count}
        for value, count in sorted(location_counts.items())
        if value
    ]

    return {
        "collections": collections,
        "sequences": sequences,
        "cloudCoverOktas": cloud_cover,
        "segmentation": segmentation,
        "artifacts": artifacts,
        "seasons": seasons,
        "timesOfDay": times_of_day,
        "locations": locations,
    }
