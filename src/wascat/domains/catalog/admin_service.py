"""Editing the catalogue.

Two rules shape everything here.

A published release is immutable. The database enforces it with a trigger, so
these functions check first only to produce a legible error rather than a
constraint violation - the rule is true whether or not this code runs.

An unmeasured value stays absent. Nothing a curator can send sets a cloud
cover: that comes from the mask. What they can supply is what a person knows
and a pipeline does not - where the camera stood, when it looked up, what it
was.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from wascat.core.errors import ConflictError, NotFoundError, ReleaseImmutableError
from wascat.domains.catalog.admin_schemas import (
    BulkImageEdit,
    CollectionCreate,
    CollectionWrite,
    ImageRecordWrite,
    ReleaseCreate,
)
from wascat.domains.catalog.models import (
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)

#: The record fields a curator may set, and the column each maps to. Cloud
#: cover is deliberately absent.
RECORD_FIELDS: dict[str, str] = {
    "captured_at": "captured_at",
    "latitude": "latitude",
    "longitude": "longitude",
    "instrument": "instrument",
    "location": "location_label",
    "season": "season_label",
    "time_of_day": "time_of_day_label",
    "sky_class": "sky_class_label",
    "custom": "custom",
}

COLLECTION_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "location_name",
    "latitude",
    "longitude",
    "instrument",
    "license",
    "citation",
    "doi",
    "methods_url",
    "publication_url",
    "position",
)


def _parse_moment(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def snapshot_collection(collection: Collection) -> dict[str, Any]:
    return {field: _plain(getattr(collection, field)) for field in COLLECTION_FIELDS} | {
        "slug": collection.slug
    }


def snapshot_record(record: ImageRecord) -> dict[str, Any]:
    return {key: _plain(getattr(record, column)) for key, column in RECORD_FIELDS.items()} | {
        "conditionTags": list(record.condition_tags or [])
    }


def _plain(value: Any) -> Any:
    """JSON-safe, for the audit log."""
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if hasattr(value, "quantize"):  # Decimal
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


async def create_collection(session: AsyncSession, payload: CollectionCreate) -> Collection:
    existing = (
        (await session.execute(select(Collection).where(Collection.slug == payload.slug)))
        .scalars()
        .first()
    )
    if existing is not None:
        raise ConflictError(f"A collection with the slug '{payload.slug}' already exists.")

    collection = Collection(slug=payload.slug)
    _apply_collection(collection, payload)
    session.add(collection)
    await session.flush()
    return collection


async def update_collection(
    session: AsyncSession, collection: Collection, payload: CollectionWrite
) -> Collection:
    _apply_collection(collection, payload)
    await session.flush()
    return collection


def _apply_collection(collection: Collection, payload: CollectionWrite) -> None:
    # exclude_unset: a field the form did not send is left alone, while a field
    # sent as null is cleared. Collapsing the two would let an edit form blank
    # every value it did not happen to render.
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "slug":
            continue
        setattr(collection, field, value)


async def get_collection_or_404(session: AsyncSession, slug: str) -> Collection:
    collection = (
        (
            await session.execute(
                select(Collection)
                .options(selectinload(Collection.releases))
                .where(Collection.slug == slug)
            )
        )
        .scalars()
        .first()
    )
    if collection is None:
        raise NotFoundError("Collection not found")
    return collection


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


async def create_release(
    session: AsyncSession, collection: Collection, payload: ReleaseCreate
) -> Release:
    clash = (
        (
            await session.execute(
                select(Release).where(
                    Release.collection_id == collection.id,
                    Release.version == payload.version,
                )
            )
        )
        .scalars()
        .first()
    )
    if clash is not None:
        raise ConflictError(f"{collection.slug} already has a release {payload.version}.")

    release = Release(
        collection_id=collection.id,
        version=payload.version,
        status=ReleaseStatus.DRAFT,
        capture_start=_parse_moment(payload.capture_start),
        capture_end=_parse_moment(payload.capture_end),
        meta={"notes": payload.notes} if payload.notes else {},
    )
    session.add(release)
    await session.flush()
    return release


async def publish_release(
    session: AsyncSession, release: Release, *, make_current: bool = True
) -> Release:
    """Draft -> Published.

    Publishing is the point after which the release cannot be edited, so it is
    a deliberate transition rather than a field someone can set while editing
    something else.
    """
    if release.status is ReleaseStatus.PUBLISHED:
        raise ConflictError("That release is already published.")
    if release.status is ReleaseStatus.RETIRED:
        raise ConflictError("A retired release cannot be published again.")

    release.status = ReleaseStatus.PUBLISHED
    release.published_at = datetime.now(UTC)

    if make_current:
        # A collection has at most one current release, so this demotes the
        # incumbent in the same transaction rather than relying on the unique
        # index to reject the second one.
        await session.execute(
            update(Release)
            .where(
                Release.collection_id == release.collection_id,
                Release.id != release.id,
                Release.current.is_(True),
            )
            .values(current=False)
        )
        await session.flush()
        release.current = True

    await session.flush()
    return release


async def retire_release(session: AsyncSession, release: Release) -> Release:
    if release.status is ReleaseStatus.DRAFT:
        raise ConflictError("A draft has nothing to retire; delete it instead.")

    release.status = ReleaseStatus.RETIRED
    release.retired_at = datetime.now(UTC)
    # Retiring the current release leaves the collection with none, which is
    # honest: there is no current release until one is published.
    release.current = False
    await session.flush()
    return release


async def get_release_or_404(session: AsyncSession, release_id: uuid.UUID) -> Release:
    release = await session.get(Release, release_id)
    if release is None:
        raise NotFoundError("Release not found")
    return release


def assert_writable(release: Release) -> None:
    """Refuse an edit to a published release, with a readable reason.

    The trigger would refuse it anyway; this exists so the dashboard shows a
    sentence rather than a constraint name.
    """
    if release.status is not ReleaseStatus.DRAFT:
        raise ReleaseImmutableError(
            f"Release {release.version} is {release.status.value.lower()}. "
            "Published releases cannot be edited - create a new release instead."
        )


# ---------------------------------------------------------------------------
# Image records
# ---------------------------------------------------------------------------


async def get_record_or_404(session: AsyncSession, record_id: str) -> ImageRecord:
    record = (
        (
            await session.execute(
                select(ImageRecord)
                .options(selectinload(ImageRecord.artifacts))
                .where(ImageRecord.id == record_id)
            )
        )
        .scalars()
        .first()
    )
    if record is None:
        raise NotFoundError("Image record not found")
    return record


async def update_record(
    session: AsyncSession, record: ImageRecord, payload: ImageRecordWrite
) -> ImageRecord:
    release = await session.get(Release, record.release_id)
    if release is not None:
        assert_writable(release)

    changes = payload.model_dump(exclude_unset=True)

    for field, column in RECORD_FIELDS.items():
        if field not in changes:
            continue
        value = changes[field]
        if field == "captured_at":
            value = _parse_moment(value)
        setattr(record, column, value)

    if "condition_tags" in changes and changes["condition_tags"] is not None:
        record.condition_tags = sorted(set(changes["condition_tags"]))

    await session.flush()
    return record


async def bulk_update_records(
    session: AsyncSession, payload: BulkImageEdit
) -> tuple[list[ImageRecord], list[str]]:
    """Apply the same provenance to many frames.

    Returns the records changed and the ids that could not be. A partial
    result is deliberate: one frame belonging to a published release should
    not silently discard an edit meant for four hundred others - but the
    curator does need to be told which ones were skipped.
    """
    records = (
        (
            await session.execute(
                select(ImageRecord)
                .options(selectinload(ImageRecord.artifacts))
                .where(ImageRecord.id.in_(payload.ids))
            )
        )
        .scalars()
        .all()
    )
    found = {record.id for record in records}
    skipped = [record_id for record_id in payload.ids if record_id not in found]

    release_ids = {record.release_id for record in records}
    releases = {
        release.id: release
        for release in (await session.execute(select(Release).where(Release.id.in_(release_ids))))
        .scalars()
        .all()
    }

    changes = payload.changes.model_dump(exclude_unset=True)
    tags = changes.pop("condition_tags", None)
    updated: list[ImageRecord] = []

    for record in records:
        release = releases.get(record.release_id)
        if release is not None and release.status is not ReleaseStatus.DRAFT:
            skipped.append(record.id)
            continue

        for field, column in RECORD_FIELDS.items():
            if field not in changes:
                continue
            value = changes[field]
            if field == "captured_at":
                value = _parse_moment(value)
            setattr(record, column, value)

        if tags is not None:
            current = set(record.condition_tags or [])
            if payload.tag_mode == "add":
                record.condition_tags = sorted(current | set(tags))
            elif payload.tag_mode == "remove":
                record.condition_tags = sorted(current - set(tags))
            else:
                record.condition_tags = sorted(set(tags))

        updated.append(record)

    await session.flush()
    return updated, skipped


async def retire_record(session: AsyncSession, record: ImageRecord) -> ImageRecord:
    """Hide a record from the public API without destroying it.

    Deleting would take the audit trail and the provenance with it. Retiring
    leaves both, and can be undone.
    """
    release = await session.get(Release, record.release_id)
    if release is not None:
        assert_writable(release)

    record.retired_at = datetime.now(UTC)
    await session.flush()
    return record


async def restore_record(session: AsyncSession, record: ImageRecord) -> ImageRecord:
    release = await session.get(Release, record.release_id)
    if release is not None:
        assert_writable(release)

    record.retired_at = None
    await session.flush()
    return record
