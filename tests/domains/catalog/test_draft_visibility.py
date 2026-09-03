"""A draft release is not part of the archive.

Releases exist so that a citation resolves to the same data forever. That only
works if the reverse also holds: data nobody has published is not yet in the
archive, however real the rows are.

This was a live bug. Adding twenty backfilled frames to a draft moved the
public total from 2,422 to 2,442 - announcing a release before anyone made it.
These tests cover every public read that counts or returns records.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.pagination import Cursor
from wascat.domains.catalog import admin_service, repository
from wascat.domains.catalog.facets import build_facets
from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)
from wascat.domains.catalog.query import parse_image_query

pytestmark = pytest.mark.db

TEST_VIDEO = "vid9200"


async def make_release(session: AsyncSession, collection: Collection, version: str) -> Release:
    release = Release(collection_id=collection.id, version=version)
    session.add(release)
    await session.flush()
    return release


async def add_record(
    session: AsyncSession, collection: Collection, release: Release
) -> ImageRecord:
    frame = uuid.uuid4().int % 100_000_000
    record = ImageRecord(
        id=f"WAS-T{uuid.uuid4().hex[:8].upper()}",
        release_id=release.id,
        collection_id=collection.id,
        video_id=TEST_VIDEO,
        frame_index=frame,
        width=640,
        height=360,
        provenance={},
    )
    session.add(record)
    await session.flush()
    session.add(
        Artifact(
            image_id=record.id,
            type="source",
            media_type="image/jpeg",
            object_key=f"frames/{TEST_VIDEO}/{frame}-source.jpg",
            checksum="a" * 64,
            bytes=1000,
            width=640,
            height=360,
        )
    )
    await session.flush()
    return record


@pytest.fixture
async def archive(session: AsyncSession) -> tuple[Collection, ImageRecord, ImageRecord]:
    """One collection with a published record and a draft one."""
    collection = Collection(slug=f"vid{uuid.uuid4().int % 100000}")
    session.add(collection)
    await session.flush()

    # Records go in while the release is still a draft, then it is published.
    # The other order is refused, which is the immutability rule working.
    published_release = await make_release(session, collection, "1.0")
    published = await add_record(session, collection, published_release)
    await session.commit()
    await admin_service.publish_release(session, published_release, make_current=False)
    await session.commit()

    draft_release = await make_release(session, collection, "1.1")
    draft = await add_record(session, collection, draft_release)
    await session.commit()

    return collection, published, draft


class TestPublicReads:
    async def test_listing_excludes_draft_records(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        collection, published, draft = archive
        query = parse_image_query({"collection": collection.slug})
        page = await repository.list_images(session, query, Cursor.start())

        ids = {row.record.id for row in page.rows}
        assert published.id in ids
        assert draft.id not in ids

    async def test_the_total_counts_only_published_records(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        # The bug that prompted these tests: the count included drafts, so the
        # archive appeared to grow the moment someone started assembling a
        # release.
        collection, _, _ = archive
        query = parse_image_query({"collection": collection.slug})
        page = await repository.list_images(session, query, Cursor.start())
        assert page.total == 1

    async def test_fetching_a_draft_record_by_id_finds_nothing(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        _, _, draft = archive
        assert await repository.get_image(session, draft.id) is None

    async def test_collection_counts_exclude_drafts(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        collection, _, _ = archive
        stats = await repository.collection_stats(session, collection.id)
        # The number the collection page prints.
        assert stats.images == 1

    async def test_the_cover_frame_is_never_a_draft(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        collection, published, _ = archive
        cover = await repository.cover_record(session, collection.id)
        assert cover is not None
        assert cover.record.id == published.id

    async def test_facets_count_only_published_records(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        collection, _, _ = archive
        facets = await build_facets(session)
        entry = next(item for item in facets["collections"] if item["value"] == collection.slug)
        # Facets that disagreed with the listings would be worse than either
        # being wrong on its own.
        assert entry["count"] == 1


class TestTheDashboardSeesDrafts:
    async def test_because_a_curator_has_to_review_what_they_are_publishing(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        collection, published, draft = archive
        query = parse_image_query({"collection": collection.slug})
        page = await repository.list_images(session, query, Cursor.start(), include_drafts=True)

        ids = {row.record.id for row in page.rows}
        assert {published.id, draft.id} <= ids
        assert page.total == 2

    async def test_and_can_open_a_draft_record(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        _, _, draft = archive
        row = await repository.get_image(session, draft.id, include_drafts=True)
        assert row is not None
        assert row.record.id == draft.id


class TestPublishingMakesThemVisible:
    async def test_a_draft_joins_the_archive_when_published(
        self, session: AsyncSession, archive: tuple[Collection, ImageRecord, ImageRecord]
    ) -> None:
        collection, _, draft = archive
        release = await session.get(Release, draft.release_id)
        assert release is not None
        assert release.status is ReleaseStatus.DRAFT

        await admin_service.publish_release(session, release)
        await session.commit()

        query = parse_image_query({"collection": collection.slug})
        page = await repository.list_images(session, query, Cursor.start())
        assert page.total == 2
        assert draft.id in {row.record.id for row in page.rows}
