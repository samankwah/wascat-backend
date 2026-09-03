"""Accepting imagery into the archive.

The upload path is where a person's file becomes a fact the archive asserts,
so most of these tests are about what it refuses and what it invalidates.

The one that matters most is the last group: replacing a mask clears the cloud
cover measured from the old one. Keeping that number would leave the archive
reporting a measurement taken from a file it no longer holds.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.errors import (
    ConflictError,
    PayloadTooLargeError,
    ReleaseImmutableError,
    UnprocessableUploadError,
)
from wascat.domains.catalog import admin_service, uploads
from wascat.domains.catalog.models import Artifact, Collection, ImageRecord, Release
from wascat.storage.local import LocalObjectStore

pytestmark = pytest.mark.db

FRAME_SIZE = (640, 360)


def encode(
    size: tuple[int, int] = FRAME_SIZE, colour: tuple[int, int, int] = (30, 50, 80)
) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> LocalObjectStore:
    # The filesystem store, so these run without Docker. The S3 implementation
    # is exercised separately.
    return LocalObjectStore(tmp_path)


#: Test records use a sequence number far above anything the archive holds.
#:
#: artifacts.object_key is unique across the whole table, and the contract
#: suite seeds the real catalogue into the same database - so a fixture using
#: vid1 with a small frame index eventually collides with a genuine
#: "frames/vid1/523-source.jpg". Reserving a high sequence keeps generated
#: keys and real keys in separate spaces.
TEST_VIDEO = "vid9000"


async def make_record(
    session: AsyncSession, *, published: bool = False, with_mask: bool = False
) -> ImageRecord:
    collection = Collection(slug=f"vid{uuid.uuid4().int % 100000}")
    session.add(collection)
    await session.flush()

    release = Release(collection_id=collection.id, version="1.0")
    session.add(release)
    await session.flush()

    record = ImageRecord(
        id=f"WAS-T{uuid.uuid4().hex[:8].upper()}",
        release_id=release.id,
        collection_id=collection.id,
        video_id=TEST_VIDEO,
        # uuid rather than a small random int: the key space has to be wide
        # enough that repeated runs against a persistent database do not
        # collide by birthday.
        frame_index=uuid.uuid4().int % 100_000_000,
        width=FRAME_SIZE[0],
        height=FRAME_SIZE[1],
        provenance={},
    )
    if with_mask:
        record.cloud_fraction = Decimal("0.525588")
        record.cloud_cover_oktas = 4
    session.add(record)
    await session.flush()

    session.add(
        Artifact(
            image_id=record.id,
            type="source",
            media_type="image/jpeg",
            object_key=f"frames/{TEST_VIDEO}/{record.frame_index}-source.jpg",
            checksum="a" * 64,
            bytes=23133,
            width=FRAME_SIZE[0],
            height=FRAME_SIZE[1],
        )
    )
    if with_mask:
        session.add(
            Artifact(
                image_id=record.id,
                type="mask",
                media_type="image/jpeg",
                object_key=f"frames/{TEST_VIDEO}/{record.frame_index}-mask.jpg",
                checksum="b" * 64,
                bytes=21265,
                width=FRAME_SIZE[0],
                height=FRAME_SIZE[1],
            )
        )
    await session.commit()

    if published:
        await admin_service.publish_release(session, release)
        await session.commit()

    return record


class TestWhatIsRefused:
    async def test_an_image_of_the_wrong_size(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session)
        with pytest.raises(UnprocessableUploadError) as caught:
            await uploads.replace_artifact(
                session, store, record=record, artifact_type="mask", data=encode((320, 180))
            )
        # A mask that does not line up pixel-for-pixel with its frame would
        # make the measured cover meaningless, and the message says so.
        assert "line up" in str(caught.value)
        assert "640x360" in str(caught.value)

    async def test_something_that_is_not_an_image(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session)
        with pytest.raises(UnprocessableUploadError):
            await uploads.replace_artifact(
                session, store, record=record, artifact_type="mask", data=b"not an image"
            )

    async def test_an_upload_into_a_published_release(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session, published=True)
        with pytest.raises(ReleaseImmutableError):
            await uploads.replace_artifact(
                session, store, record=record, artifact_type="mask", data=encode()
            )

    async def test_a_file_over_the_size_limit(
        self, session: AsyncSession, store: LocalObjectStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from wascat.core import config

        record = await make_record(session)
        settings = config.get_settings().model_copy(update={"max_upload_bytes": 100})
        monkeypatch.setattr(uploads, "get_settings", lambda: settings)

        with pytest.raises(PayloadTooLargeError):
            await uploads.replace_artifact(
                session, store, record=record, artifact_type="mask", data=encode()
            )

    async def test_re_uploading_a_byte_identical_file(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session)
        data = encode()
        await uploads.replace_artifact(
            session, store, record=record, artifact_type="mask", data=data
        )
        await session.commit()

        # Saying "that is already there" is more useful than reporting a
        # successful no-op that changed nothing.
        with pytest.raises(ConflictError, match="byte-for-byte identical"):
            await uploads.replace_artifact(
                session, store, record=record, artifact_type="mask", data=data
            )


class TestAttaching:
    async def test_stores_the_file_and_records_what_it_is(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session)
        data = encode()

        result = await uploads.replace_artifact(
            session, store, record=record, artifact_type="mask", data=data
        )
        await session.commit()

        assert result.replaced is False
        assert (result.width, result.height) == FRAME_SIZE
        assert result.bytes == len(data)
        # The object is actually in storage, not merely recorded as such.
        assert await store.head(result.object_key) is not None

    async def test_sets_the_mask_flag_the_public_api_reads(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session)
        assert record.has_mask is False

        await uploads.replace_artifact(
            session, store, record=record, artifact_type="mask", data=encode()
        )
        await session.commit()
        await session.refresh(record)

        # Maintained by trigger from the artifact rows, not written by hand.
        assert record.has_mask is True

    async def test_generates_the_dashboard_renditions(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session)
        result = await uploads.replace_artifact(
            session, store, record=record, artifact_type="mask", data=encode()
        )
        await session.commit()

        assert len(result.derivatives) == 2
        for key in result.derivatives:
            assert await store.head(key) is not None

    async def test_derivatives_are_not_artifact_rows(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session)
        await uploads.replace_artifact(
            session, store, record=record, artifact_type="mask", data=encode()
        )
        await session.commit()
        await session.refresh(record, ["artifacts"])

        # Counting them would change collection.artifacts, a number the public
        # site prints.
        assert {artifact.type for artifact in record.artifacts} == {"source", "mask"}


class TestReplacingAMaskInvalidatesItsMeasurement:
    """The most consequential behaviour in the upload path.

    Cloud cover is measured *from* the mask. Replace the mask and the number
    describes a file the archive no longer holds, so keeping it would be the
    archive asserting a measurement nobody made of anything present.
    """

    async def test_the_measurement_is_cleared(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session, with_mask=True)
        assert record.cloud_cover_oktas == 4

        await uploads.replace_artifact(
            session,
            store,
            record=record,
            artifact_type="mask",
            data=encode(colour=(200, 200, 200)),
        )
        await session.commit()
        await session.refresh(record)

        # Absent, not zero. Zero would be a clear sky nobody observed.
        assert record.cloud_fraction is None
        assert record.cloud_cover_oktas is None

    async def test_and_why_is_recorded_in_provenance(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session, with_mask=True)
        await uploads.replace_artifact(
            session,
            store,
            record=record,
            artifact_type="mask",
            data=encode(colour=(210, 210, 210)),
        )
        await session.commit()
        await session.refresh(record)

        assert record.provenance.get("maskReplaced") is True
        assert "Re-run the pipeline" in record.provenance["measurementCleared"]

    async def test_replacing_the_source_leaves_the_measurement_alone(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        # The measurement came from the mask, and the mask has not changed.
        record = await make_record(session, with_mask=True)
        await uploads.replace_artifact(
            session,
            store,
            record=record,
            artifact_type="source",
            data=encode(colour=(90, 120, 150)),
        )
        await session.commit()
        await session.refresh(record)

        assert record.cloud_cover_oktas == 4


class TestRemoving:
    async def test_removing_the_mask_removes_its_measurement(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session, with_mask=True)
        await uploads.delete_artifact(session, store, record=record, artifact_type="mask")
        await session.commit()
        await session.refresh(record)

        assert record.cloud_cover_oktas is None
        assert record.has_mask is False

    async def test_removing_the_last_artifact_is_refused(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session)

        # The deferred constraint would reject it at commit anyway; refusing
        # here means the message explains what to do instead.
        with pytest.raises(ConflictError, match="Retire the record instead"):
            await uploads.delete_artifact(session, store, record=record, artifact_type="source")

    async def test_removing_from_a_published_release_is_refused(
        self, session: AsyncSession, store: LocalObjectStore
    ) -> None:
        record = await make_record(session, published=True, with_mask=True)
        with pytest.raises(ReleaseImmutableError):
            await uploads.delete_artifact(session, store, record=record, artifact_type="mask")
