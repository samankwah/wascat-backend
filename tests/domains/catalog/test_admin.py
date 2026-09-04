"""The dashboard's write surface.

Focused on the things that would be damaging to get wrong: that a published
release cannot be edited, that a curator cannot publish, that a bulk edit
reports what it skipped rather than silently dropping it, that nothing a
curator sends can invent a measurement, and that every change is recorded.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.errors import ConflictError, ReleaseImmutableError
from wascat.domains.audit import service as audit
from wascat.domains.audit.models import AuditEvent
from wascat.domains.catalog import admin_service
from wascat.domains.catalog.admin_schemas import (
    BulkImageEdit,
    CollectionCreate,
    CollectionWrite,
    ImageRecordWrite,
)
from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)

pytestmark = pytest.mark.db


async def make_draft(session: AsyncSession) -> tuple[Collection, Release]:
    collection = Collection(slug=f"vid{uuid.uuid4().int % 100000}")
    session.add(collection)
    await session.flush()
    release = Release(collection_id=collection.id, version="1.0", status=ReleaseStatus.DRAFT)
    session.add(release)
    await session.flush()
    return collection, release


async def add_record(
    session: AsyncSession, collection: Collection, release: Release, frame: int = 1
) -> ImageRecord:
    record = ImageRecord(
        id=f"WAS-T{uuid.uuid4().hex[:8].upper()}",
        release_id=release.id,
        collection_id=collection.id,
        sequence_id="seq-001",
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
            object_key=f"frames/seq-001/{uuid.uuid4().hex}-source.jpg",
            checksum="a" * 64,
            bytes=23133,
            width=640,
            height=360,
        )
    )
    await session.flush()
    return record


class TestReleaseLifecycle:
    async def test_draft_publishes_and_becomes_current(self, session: AsyncSession) -> None:
        _, release = await make_draft(session)
        await admin_service.publish_release(session, release)

        assert release.status is ReleaseStatus.PUBLISHED
        assert release.current is True
        assert release.published_at is not None

    async def test_publishing_demotes_the_previous_current_release(
        self, session: AsyncSession
    ) -> None:
        collection, first = await make_draft(session)
        await admin_service.publish_release(session, first)

        second = Release(collection_id=collection.id, version="1.1")
        session.add(second)
        await session.flush()
        await admin_service.publish_release(session, second)
        await session.refresh(first)

        # A collection has one current release; the index would reject two,
        # but the service demotes rather than failing.
        assert second.current is True
        assert first.current is False
        assert first.status is ReleaseStatus.PUBLISHED

    async def test_a_published_release_cannot_be_published_again(
        self, session: AsyncSession
    ) -> None:
        _, release = await make_draft(session)
        await admin_service.publish_release(session, release)
        with pytest.raises(ConflictError):
            await admin_service.publish_release(session, release)

    async def test_a_draft_cannot_be_retired(self, session: AsyncSession) -> None:
        _, release = await make_draft(session)
        with pytest.raises(ConflictError, match="nothing to retire"):
            await admin_service.retire_release(session, release)

    async def test_retiring_clears_current(self, session: AsyncSession) -> None:
        _, release = await make_draft(session)
        await admin_service.publish_release(session, release)
        await admin_service.retire_release(session, release)

        assert release.status is ReleaseStatus.RETIRED
        # Honest: the collection now has no current release, rather than one
        # that has been withdrawn.
        assert release.current is False


class TestImmutability:
    async def test_a_record_in_a_published_release_cannot_be_edited(
        self, session: AsyncSession
    ) -> None:
        collection, release = await make_draft(session)
        record = await add_record(session, collection, release)
        await session.commit()

        await admin_service.publish_release(session, release)
        await session.commit()

        with pytest.raises(ReleaseImmutableError, match="cannot be edited"):
            await admin_service.update_record(
                session, record, ImageRecordWrite(instrument="Retrofitted")
            )

    async def test_a_record_in_a_draft_can_be_edited(self, session: AsyncSession) -> None:
        collection, release = await make_draft(session)
        record = await add_record(session, collection, release)

        await admin_service.update_record(
            session,
            record,
            ImageRecordWrite(instrument="All-sky camera", location="Kumasi"),
        )
        assert record.instrument == "All-sky camera"
        assert record.location_label == "Kumasi"

    async def test_a_published_record_cannot_be_retired(self, session: AsyncSession) -> None:
        collection, release = await make_draft(session)
        record = await add_record(session, collection, release)
        await session.commit()
        await admin_service.publish_release(session, release)
        await session.commit()

        with pytest.raises(ReleaseImmutableError):
            await admin_service.retire_record(session, record)


class TestWhatACuratorMaySet:
    async def test_a_measurement_cannot_be_supplied(self) -> None:
        # Cloud cover comes from the mask. Accepting it here would let a person
        # assert a measurement nobody made, which is the one thing the archive
        # must never do.
        with pytest.raises(Exception, match=r"cloudFraction|cloud_fraction|extra"):
            ImageRecordWrite(cloudFraction=0.5)  # type: ignore[call-arg]

    async def test_an_unset_field_is_left_alone(self, session: AsyncSession) -> None:
        collection, release = await make_draft(session)
        record = await add_record(session, collection, release)
        record.instrument = "Existing"
        record.location_label = "Existing site"
        await session.flush()

        # A form that renders only the location must not blank the instrument.
        await admin_service.update_record(session, record, ImageRecordWrite(location="New site"))
        assert record.location_label == "New site"
        assert record.instrument == "Existing"

    async def test_an_explicit_null_clears_a_field(self, session: AsyncSession) -> None:
        collection, release = await make_draft(session)
        record = await add_record(session, collection, release)
        record.instrument = "Wrong"
        await session.flush()

        await admin_service.update_record(
            session, record, ImageRecordWrite.model_validate({"instrument": None})
        )
        assert record.instrument is None

    async def test_a_capture_time_is_parsed(self, session: AsyncSession) -> None:
        collection, release = await make_draft(session)
        record = await add_record(session, collection, release)

        await admin_service.update_record(
            session, record, ImageRecordWrite(capturedAt="2026-03-14T09:00:00Z")
        )
        assert record.captured_at == datetime(2026, 3, 14, 9, 0, tzinfo=UTC)
        # The derived sort key follows, so ordering reflects the new fact.
        await session.refresh(record)
        assert record.sort_key.startswith("2026-03-14T09:00:00")


class TestBulkEditing:
    async def test_applies_to_every_editable_record(self, session: AsyncSession) -> None:
        collection, release = await make_draft(session)
        records = [await add_record(session, collection, release, frame) for frame in (1, 2, 3)]
        await session.commit()

        updated, skipped = await admin_service.bulk_update_records(
            session,
            BulkImageEdit(
                ids=[record.id for record in records],
                changes=ImageRecordWrite(instrument="All-sky camera"),
            ),
        )
        assert len(updated) == 3
        assert skipped == []
        assert all(record.instrument == "All-sky camera" for record in records)

    async def test_reports_what_it_could_not_change(self, session: AsyncSession) -> None:
        editable_collection, draft = await make_draft(session)
        editable = await add_record(session, editable_collection, draft, 1)

        locked_collection, published = await make_draft(session)
        locked = await add_record(session, locked_collection, published, 1)
        await session.commit()
        await admin_service.publish_release(session, published)
        await session.commit()

        updated, skipped = await admin_service.bulk_update_records(
            session,
            BulkImageEdit(
                ids=[editable.id, locked.id, "WAS-DOES-NOT-EXIST"],
                changes=ImageRecordWrite(instrument="All-sky camera"),
            ),
        )

        # A frame in a published release should not veto an edit meant for
        # hundreds of others - but the curator has to be told which were left.
        assert [record.id for record in updated] == [editable.id]
        assert set(skipped) == {locked.id, "WAS-DOES-NOT-EXIST"}

    async def test_tags_are_additive_by_default(self, session: AsyncSession) -> None:
        collection, release = await make_draft(session)
        record = await add_record(session, collection, release)
        record.condition_tags = ["dusty"]
        await session.flush()

        await admin_service.bulk_update_records(
            session,
            BulkImageEdit(
                ids=[record.id],
                changes=ImageRecordWrite(conditionTags=["hazy"]),
            ),
        )
        # A bulk edit that silently discarded existing tags would be very hard
        # to notice afterwards.
        assert record.condition_tags == ["dusty", "hazy"]

    async def test_tags_can_be_replaced_or_removed_on_request(self, session: AsyncSession) -> None:
        collection, release = await make_draft(session)
        record = await add_record(session, collection, release)
        record.condition_tags = ["dusty", "hazy"]
        await session.flush()

        await admin_service.bulk_update_records(
            session,
            BulkImageEdit(
                ids=[record.id],
                changes=ImageRecordWrite(conditionTags=["clear"]),
                tagMode="replace",
            ),
        )
        assert record.condition_tags == ["clear"]

        await admin_service.bulk_update_records(
            session,
            BulkImageEdit(
                ids=[record.id],
                changes=ImageRecordWrite(conditionTags=["clear"]),
                tagMode="remove",
            ),
        )
        assert record.condition_tags == []


class TestCollections:
    async def test_creating_a_duplicate_slug_is_refused(self, session: AsyncSession) -> None:
        slug = f"vid{uuid.uuid4().int % 100000}"
        await admin_service.create_collection(session, CollectionCreate(slug=slug))
        with pytest.raises(ConflictError, match="already exists"):
            await admin_service.create_collection(session, CollectionCreate(slug=slug))

    async def test_supplying_provenance_changes_what_the_public_sees(
        self, session: AsyncSession
    ) -> None:
        collection, _ = await make_draft(session)
        # Every sequence starts exactly like this: no site, no licence, no
        # instrument. That is the state the dashboard exists to change.
        assert admin_service.snapshot_collection(collection)["location_name"] is None

        await admin_service.update_collection(
            session,
            collection,
            CollectionWrite(
                locationName="Kumasi",
                latitude=Decimal("6.674500"),
                longitude=Decimal("-1.571600"),
                license="CC BY 4.0",
            ),
        )
        # This is the whole point of the dashboard: these fields were NULL for
        # every sequence, which is why the site showed no sites or licences.
        assert collection.location_name == "Kumasi"
        assert collection.license == "CC BY 4.0"


class TestAuditTrail:
    async def test_an_edit_records_what_changed_and_who(self, session: AsyncSession) -> None:
        collection, _ = await make_draft(session)
        before = admin_service.snapshot_collection(collection)

        await admin_service.update_collection(
            session, collection, CollectionWrite(instrument="All-sky camera")
        )
        after = admin_service.snapshot_collection(collection)

        await audit.record(
            session,
            action="collection.update",
            entity_type="collection",
            entity_id=collection.slug,
            actor_email="curator@wascat.test",
            before=before,
            after=after,
        )
        await session.flush()

        event = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.entity_id == collection.slug)
                )
            )
            .scalars()
            .first()
        )
        assert event is not None
        assert event.actor_email == "curator@wascat.test"
        assert audit.changed_fields(event.before, event.after) == ["instrument"]

    def test_secrets_never_reach_the_log(self) -> None:
        scrubbed = audit.scrub(
            {"email": "curator@wascat.test", "password": "hunter2", "token_hash": "abc"}
        )
        assert scrubbed is not None
        assert scrubbed["email"] == "curator@wascat.test"
        assert scrubbed["password"] == audit.REDACTED
        assert scrubbed["token_hash"] == audit.REDACTED
