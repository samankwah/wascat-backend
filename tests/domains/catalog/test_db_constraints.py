"""The domain invariants, asserted against a real PostgreSQL.

The service layer checks these too, but a check in application code is a
convention: it holds for the paths that remember to call it. These tests prove
the rules hold at the storage layer, so they survive a bulk import, an admin
endpoint someone adds later, or a psql session during an incident.

Note the ``commit()`` calls. DEFERRED constraints fire when the transaction (or
here, the savepoint) is released, so a test that only ``flush()``es would pass
without ever exercising them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.db import run_deferred_checks
from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)

pytestmark = pytest.mark.db

CHECKSUM_A = "a" * 64
CHECKSUM_B = "b" * 64


async def make_release(
    session: AsyncSession, *, status: ReleaseStatus = ReleaseStatus.DRAFT
) -> Release:
    collection = Collection(slug=f"vid{uuid.uuid4().int % 100000}", title="Test sequence")
    session.add(collection)
    await session.flush()
    release = Release(
        collection_id=collection.id,
        version="1.0",
        status=status,
        published_at=datetime.now(UTC) if status is ReleaseStatus.PUBLISHED else None,
    )
    session.add(release)
    await session.flush()
    return release


def make_record(release: Release, **overrides: Any) -> ImageRecord:
    defaults: dict[str, Any] = {
        "id": f"WAS-T{uuid.uuid4().hex[:8].upper()}",
        "release_id": release.id,
        "collection_id": release.collection_id,
        "video_id": "vid1",
        "frame_index": 5,
        "width": 640,
        "height": 360,
        "provenance": {},
    }
    return ImageRecord(**{**defaults, **overrides})


def make_artifact(record_id: str, artifact_type: str, **overrides: Any) -> Artifact:
    defaults: dict[str, Any] = {
        "image_id": record_id,
        "type": artifact_type,
        "media_type": "image/jpeg",
        "object_key": f"frames/vid1/{uuid.uuid4().hex}-{artifact_type}.jpg",
        "checksum": CHECKSUM_A if artifact_type == "source" else CHECKSUM_B,
        "bytes": 23133,
        "width": 640,
        "height": 360,
    }
    return Artifact(**{**defaults, **overrides})


class TestInvariant2SourceMaskOrBoth:
    """A record holds a source, a mask, or both - never neither."""

    async def test_record_with_no_artifacts_is_rejected_at_commit(
        self, session: AsyncSession
    ) -> None:
        release = await make_release(session)
        session.add(make_record(release))
        with pytest.raises(DBAPIError, match="neither a source frame nor a mask"):
            await run_deferred_checks(session)

    @pytest.mark.parametrize("artifact_type", ["source", "mask"])
    async def test_a_single_artifact_is_enough(
        self, session: AsyncSession, artifact_type: str
    ) -> None:
        release = await make_release(session)
        record = make_record(release)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, artifact_type))
        await session.commit()

        await session.refresh(record)
        assert record.has_source is (artifact_type == "source")
        assert record.has_mask is (artifact_type == "mask")

    async def test_deleting_the_last_artifact_is_rejected(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release)
        session.add(record)
        await session.flush()
        artifact = make_artifact(record.id, "source")
        session.add(artifact)
        await session.commit()

        await session.delete(artifact)
        with pytest.raises(DBAPIError, match="neither a source frame nor a mask"):
            await run_deferred_checks(session)


class TestInvariant3And4Measurement:
    """Cloud cover comes from a mask, and the fraction and bucket travel together."""

    async def test_cloud_fraction_without_a_mask_is_rejected(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release, cloud_fraction=Decimal("0.525588"), cloud_cover_oktas=4)
        session.add(record)
        await session.flush()
        # Only a source frame, so there is no mask to have measured.
        session.add(make_artifact(record.id, "source"))
        with pytest.raises(DBAPIError, match="reports cloud cover without a mask"):
            await run_deferred_checks(session)

    async def test_a_fraction_without_its_bucket_is_rejected(self, session: AsyncSession) -> None:
        release = await make_release(session)
        session.add(make_record(release, cloud_fraction=Decimal("0.5")))
        with pytest.raises(IntegrityError, match="measurement_pairing"):
            await session.flush()

    async def test_a_bucket_without_its_fraction_is_rejected(self, session: AsyncSession) -> None:
        release = await make_release(session)
        session.add(make_record(release, cloud_cover_oktas=4))
        with pytest.raises(IntegrityError, match="measurement_pairing"):
            await session.flush()

    async def test_a_bucket_that_does_not_follow_from_the_fraction_is_rejected(
        self, session: AsyncSession
    ) -> None:
        # 0.525588 * 8 = 4.2, which rounds to 4, not 7.
        release = await make_release(session)
        session.add(make_record(release, cloud_fraction=Decimal("0.525588"), cloud_cover_oktas=7))
        with pytest.raises(IntegrityError, match="okta_formula"):
            await session.flush()

    async def test_a_measured_record_with_a_mask_is_accepted(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release, cloud_fraction=Decimal("0.525588"), cloud_cover_oktas=4)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, "source"))
        session.add(make_artifact(record.id, "mask"))
        await session.commit()
        await session.refresh(record)
        assert record.has_source
        assert record.has_mask


class TestInvariant5Dimensions:
    async def test_a_mismatched_source_is_rejected(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, "source", width=1280, height=720))
        with pytest.raises(DBAPIError, match="does not match the dimensions"):
            await run_deferred_checks(session)

    async def test_a_thumbnail_may_differ(self, session: AsyncSession) -> None:
        # A thumbnail is a different size by definition; the rule covers only
        # the artifacts that depict the same pixels as the record.
        release = await make_release(session)
        record = make_record(release)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, "source"))
        session.add(make_artifact(record.id, "thumbnail", width=320, height=180))
        await run_deferred_checks(session)
        await session.commit()


class TestInvariant6Uniqueness:
    async def test_two_artifacts_of_the_same_type_are_rejected(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, "source"))
        session.add(make_artifact(record.id, "source"))
        with pytest.raises(IntegrityError, match="uq_artifacts_image_id_type"):
            await session.flush()


class TestInvariant9PublishedReleasesAreImmutable:
    async def test_cannot_add_a_record_to_a_published_release(self, session: AsyncSession) -> None:
        release = await make_release(session, status=ReleaseStatus.PUBLISHED)
        session.add(make_record(release))
        with pytest.raises(DBAPIError, match="published releases are immutable"):
            await session.flush()

    async def test_cannot_edit_a_record_in_a_published_release(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, "source"))
        await session.commit()

        release.status = ReleaseStatus.PUBLISHED
        release.published_at = datetime.now(UTC)
        await session.commit()

        record.instrument = "Retrofitted after publication"
        with pytest.raises(DBAPIError, match="published releases are immutable"):
            await session.commit()


class TestInvariant10Formats:
    @pytest.mark.parametrize("bad_id", ["was-v01-f5", "V01-F5", "WAS_V01_F5", "WAS-v01"])
    async def test_rejects_a_malformed_record_id(self, session: AsyncSession, bad_id: str) -> None:
        release = await make_release(session)
        session.add(make_record(release, id=bad_id))
        with pytest.raises(IntegrityError, match="id_format"):
            await session.flush()

    @pytest.mark.parametrize("bad_video", ["video1", "vid", "VID1"])
    async def test_rejects_a_malformed_sequence_id(
        self, session: AsyncSession, bad_video: str
    ) -> None:
        release = await make_release(session)
        session.add(make_record(release, video_id=bad_video))
        with pytest.raises(IntegrityError, match="video_id_format"):
            await session.flush()

    async def test_rejects_a_checksum_that_is_not_sha256(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, "source", checksum="deadbeef"))
        with pytest.raises(IntegrityError, match="checksum_sha256"):
            await session.flush()


class TestDerivedColumns:
    async def test_sort_key_falls_back_to_the_sequence_position(
        self, session: AsyncSession
    ) -> None:
        release = await make_release(session)
        record = make_record(release, video_id="vid1", frame_index=5)
        session.add(record)
        await session.flush()
        await session.refresh(record)
        assert record.sort_key == "001-0000005"
        assert record.video_number == 1

    async def test_sort_key_uses_the_capture_time_when_there_is_one(
        self, session: AsyncSession
    ) -> None:
        release = await make_release(session)
        record = make_record(release, captured_at=datetime(2026, 3, 14, 9, 0, tzinfo=UTC))
        session.add(record)
        await session.flush()
        await session.refresh(record)
        assert record.sort_key == "2026-03-14T09:00:00.000Z"
        # A timestamped record sorts above an untimestamped one because an ISO
        # year starts with '2' and a zero-padded sequence number starts with
        # '0'. That holds for every sequence up to vid199; the archive has 11.
        assert record.sort_key > "011-9999999"

    async def test_video_number_orders_numerically_not_lexically(
        self, session: AsyncSession
    ) -> None:
        release = await make_release(session)
        created = []
        for video_id, frame in (("vid2", 1), ("vid10", 1)):
            record = make_record(release, video_id=video_id, frame_index=frame)
            session.add(record)
            created.append(record)
        await session.flush()
        for record in created:
            await session.refresh(record)

        # "vid10" must sort after "vid2", which zero padding guarantees; the
        # lexical ordering of the ids themselves would put it before.
        keys = sorted(record.sort_key for record in created)
        assert keys == ["002-0000001", "010-0000001"]

    async def test_search_text_reproduces_the_typescript_join(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release, cloud_fraction=Decimal("0.525588"), cloud_cover_oktas=4)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, "source"))
        session.add(make_artifact(record.id, "mask"))
        await session.commit()
        await session.refresh(record)

        # [id, videoId, frameIndex, location, ...tags].join(" ").toLowerCase()
        # with tags = [videoId, oktaLabel, pairing]. The empty location leaves
        # a doubled space, exactly as Array.join does.
        assert record.search_text == (f"{record.id.lower()} vid1 5  vid1 4/8 source + mask")

    async def test_search_text_marks_an_unsegmented_frame(self, session: AsyncSession) -> None:
        release = await make_release(session)
        record = make_record(release)
        session.add(record)
        await session.flush()
        session.add(make_artifact(record.id, "source"))
        await session.commit()
        await session.refresh(record)
        assert "unsegmented" in record.search_text
        assert "source only" in record.search_text
