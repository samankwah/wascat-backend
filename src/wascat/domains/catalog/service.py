"""Assembling the public payloads.

The presenters know how to render a row; this module knows which rows to
gather. Keeping the two apart means the contract-sensitive string building
stays in one file that nobody needs to touch when a query changes.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wascat.domains.catalog import repository
from wascat.domains.catalog.models import Collection
from wascat.domains.catalog.presenters import (
    collection_to_json,
    record_to_json,
    release_to_json,
)
from wascat.domains.catalog.repository import RecordRow


def render_record(row: RecordRow) -> dict[str, Any]:
    return record_to_json(
        row.record,
        collection_slug=row.collection_slug,
        release_version=row.release_version,
    )


async def render_collection(session: AsyncSession, collection: Collection) -> dict[str, Any]:
    stats = await repository.collection_stats(session, collection.id)
    cover = await repository.cover_record(session, collection.id)
    counts = await repository.release_image_counts(session, collection.id)
    releases = await repository.published_releases(session, collection.id)

    rendered_cover = render_record(cover) if cover else None

    return collection_to_json(
        collection,
        video_ids=stats.video_ids,
        images=stats.images,
        artifacts=stats.artifacts,
        with_source=stats.with_source,
        segmented=stats.segmented,
        min_frame=stats.min_frame,
        max_frame=stats.max_frame,
        cover_image=rendered_cover["image"] if rendered_cover else None,
        cover_alt=rendered_cover["alt"] if rendered_cover else None,
        releases=[
            release_to_json(
                release,
                images=counts.get(release.id, (0, 0))[0],
                total_bytes=counts.get(release.id, (0, 0))[1],
            )
            for release in releases
        ],
    )


async def render_collections(session: AsyncSession) -> list[dict[str, Any]]:
    collections = await repository.list_collections(session)
    return [await render_collection(session, collection) for collection in collections]


async def render_collection_summaries(session: AsyncSession) -> list[dict[str, Any]]:
    """The listing shape: releases replaced by the single current release."""
    rendered = []
    for payload in await render_collections(session):
        releases = payload.pop("releases", [])
        current = next((release for release in releases if release.get("current")), None)
        payload["currentRelease"] = current
        rendered.append(payload)
    return rendered
