"""Loading the observer's labels and a model's readings.

The label table is the archive's ground truth for cloud type, and the only
place a per-frame cloud genus comes from. So these tests are mostly about what
the loader refuses to do: invent a vocabulary term for a code nobody defined,
overwrite a measurement with an observation, create a record for a tag that
does not exist, or store a truncated probability vector as though it were a
whole distribution.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageClassPrediction,
    ImageRecord,
    Release,
)
from wascat.domains.ingest import cloud_types, labels
from wascat.domains.vocab.models import VocabKind, VocabularyTerm

pytestmark = pytest.mark.db

TEST_SEQUENCE = "seq-920"


async def make_record(
    session: AsyncSession,
    *,
    frame_index: int = 1,
    cloud_fraction: Decimal | None = None,
    status: str = "DRAFT",
) -> ImageRecord:
    """One record, with a mask when it carries a measurement.

    The measured pair is only legal on a segmented frame, and `has_mask` is
    derived from the artifact rows, so the mask has to be real.

    Draft by default so a test can write without arguing with the
    published-release guard; the tests that are *about* that guard ask for a
    published release explicitly.
    """
    suffix = uuid.uuid4().hex[:8]
    collection = Collection(slug=f"seq-{uuid.uuid4().int % 1000:03d}-{suffix}")
    session.add(collection)
    await session.flush()

    # Always born a draft: a published release refuses inserts too, so a
    # record that is meant to end up published has to be written first and
    # published after. That is also the real sequence of events.
    release = Release(collection_id=collection.id, version="1.0", status="DRAFT")
    session.add(release)
    await session.flush()

    oktas = None if cloud_fraction is None else min(8, max(0, round(float(cloud_fraction) * 8)))
    record = ImageRecord(
        id=f"WAS-V92-F{frame_index}{suffix.upper()}",
        release_id=release.id,
        collection_id=collection.id,
        sequence_id=TEST_SEQUENCE,
        frame_index=frame_index,
        cloud_fraction=cloud_fraction,
        cloud_cover_oktas=oktas,
        width=640,
        height=360,
    )
    session.add(record)
    await session.flush()

    session.add(
        Artifact(
            image_id=record.id,
            type="source",
            media_type="image/jpeg",
            object_key=f"frames/{TEST_SEQUENCE}/{record.id}-source.jpg",
            checksum="a" * 64,
            bytes=1024,
        )
    )
    if cloud_fraction is not None:
        session.add(
            Artifact(
                image_id=record.id,
                type="mask",
                media_type="image/jpeg",
                object_key=f"frames/{TEST_SEQUENCE}/{record.id}-mask.jpg",
                checksum="b" * 64,
                bytes=1024,
            )
        )
    await session.flush()

    if status == "PUBLISHED":
        release.status = "PUBLISHED"
        release.published_at = datetime.now(UTC)
        release.current = True
        await session.flush()

    return record


def write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


class TestReadingTheLabelTable:
    def test_the_three_columns_are_tag_type_and_oktas(self, tmp_path: Path) -> None:
        table = write(tmp_path, "labels.csv", "WAS-V11-F1892,SC,07\n")
        (row,) = labels.parse_labels(table)
        assert (row.tag, row.code, row.oktas) == ("WAS-V11-F1892", "SC", 7)

    def test_a_header_row_is_not_read_as_data(self, tmp_path: Path) -> None:
        table = write(tmp_path, "labels.csv", "tag,type,oktas\nWAS-V11-F1892,SC,07\n")
        assert [row.tag for row in labels.parse_labels(table)] == ["WAS-V11-F1892"]

    def test_tabs_are_read_as_well_as_commas(self, tmp_path: Path) -> None:
        table = write(tmp_path, "labels.tsv", "WAS-V11-F1892\tSC\t07\nWAS-V11-F1893\tCU\t03\n")
        assert [row.code for row in labels.parse_labels(table)] == ["SC", "CU"]

    def test_a_tag_taken_from_a_directory_listing_still_matches(self, tmp_path: Path) -> None:
        table = write(tmp_path, "labels.csv", "was-v11-f1892.jpg,sc,07\n")
        (row,) = labels.parse_labels(table)
        assert row.tag == "WAS-V11-F1892"

    def test_a_zero_padded_okta_is_a_number_not_an_octal(self, tmp_path: Path) -> None:
        table = write(tmp_path, "labels.csv", "WAS-V11-F1,SC,08\n")
        (row,) = labels.parse_labels(table)
        assert row.oktas == 8

    def test_a_missing_okta_column_is_allowed(self, tmp_path: Path) -> None:
        table = write(tmp_path, "labels.csv", "WAS-V11-F1,SC\n")
        (row,) = labels.parse_labels(table)
        assert row.oktas is None

    def test_cloud_cover_beyond_eight_eighths_is_refused(self, tmp_path: Path) -> None:
        table = write(tmp_path, "labels.csv", "WAS-V11-F1,SC,9\n")
        with pytest.raises(labels.LabelFormatError, match="eighths"):
            labels.parse_labels(table)

    def test_an_unknown_cloud_code_names_itself_and_the_known_ones(self, tmp_path: Path) -> None:
        table = write(tmp_path, "labels.csv", "WAS-V11-F1,XX,04\n")
        with pytest.raises(cloud_types.UnknownCloudCodeError, match="'XX'"):
            labels.parse_labels(table)


class TestApplyingLabels:
    async def test_the_genus_lands_on_the_record(self, session: AsyncSession) -> None:
        record = await make_record(session)
        await labels.apply_labels(session, [labels.LabelRow(record.id, "SC", 7, 1)])
        await session.refresh(record)
        assert record.sky_class_label == "Stratocumulus"

    async def test_the_observed_count_lands_in_its_own_column(self, session: AsyncSession) -> None:
        record = await make_record(session)
        await labels.apply_labels(session, [labels.LabelRow(record.id, "SC", 7, 1)])
        await session.refresh(record)
        assert record.observed_cloud_cover_oktas == 7

    async def test_the_measured_cover_is_not_touched(self, session: AsyncSession) -> None:
        # The observer says 7/8; the mask measured 2/8. Both survive: the
        # disagreement is the point, and reconciling it here would destroy the
        # only evidence that the segmentation is off on this frame.
        record = await make_record(session, cloud_fraction=Decimal("0.25"))
        await labels.apply_labels(session, [labels.LabelRow(record.id, "SC", 7, 1)])
        await session.refresh(record)
        assert record.cloud_cover_oktas == 2
        assert record.cloud_fraction == Decimal("0.250000")
        assert record.observed_cloud_cover_oktas == 7

    async def test_the_vocabulary_gains_only_the_codes_the_table_uses(
        self, session: AsyncSession
    ) -> None:
        record = await make_record(session)
        report = await labels.apply_labels(session, [labels.LabelRow(record.id, "CB", 8, 1)])
        terms = (
            (
                await session.execute(
                    select(VocabularyTerm.label).where(VocabularyTerm.kind == VocabKind.SKY_CLASS)
                )
            )
            .scalars()
            .all()
        )
        assert report.terms_created == ["Cumulonimbus"]
        assert list(terms) == ["Cumulonimbus"]

    async def test_a_second_load_reuses_the_term_it_created(self, session: AsyncSession) -> None:
        first = await make_record(session, frame_index=1)
        second = await make_record(session, frame_index=2)
        await labels.apply_labels(session, [labels.LabelRow(first.id, "SC", 7, 1)])
        report = await labels.apply_labels(session, [labels.LabelRow(second.id, "SC", 5, 1)])
        count = (
            await session.execute(
                select(VocabularyTerm).where(VocabularyTerm.kind == VocabKind.SKY_CLASS)
            )
        ).scalars()
        assert report.terms_created == []
        assert len(list(count)) == 1

    async def test_a_tag_with_no_record_is_reported_not_created(
        self, session: AsyncSession
    ) -> None:
        report = await labels.apply_labels(session, [labels.LabelRow("WAS-V99-F404", "SC", 7, 1)])
        found = (
            await session.execute(select(ImageRecord).where(ImageRecord.id == "WAS-V99-F404"))
        ).scalar_one_or_none()
        assert report.unknown_tags == ["WAS-V99-F404"]
        assert found is None

    async def test_loading_the_same_table_twice_changes_nothing(
        self, session: AsyncSession
    ) -> None:
        record = await make_record(session)
        rows = [labels.LabelRow(record.id, "SC", 7, 1)]
        await labels.apply_labels(session, rows)
        report = await labels.apply_labels(session, rows)
        assert (report.applied, report.unchanged) == (0, 1)

    async def test_a_published_record_is_labelled_only_when_asked(
        self, session: AsyncSession
    ) -> None:
        # Every record in the real archive is published, so this is the
        # ordinary case, not an edge one - but lifting the immutability guard
        # stays something the caller does on purpose.
        record = await make_record(session, status="PUBLISHED")
        await labels.apply_labels(
            session, [labels.LabelRow(record.id, "SC", 7, 1)], allow_published=True
        )
        await session.refresh(record)
        assert record.sky_class_label == "Stratocumulus"

    async def test_a_published_record_is_otherwise_refused(self, session: AsyncSession) -> None:
        record = await make_record(session, status="PUBLISHED")
        with pytest.raises(DBAPIError, match="published releases are immutable"):
            await labels.apply_labels(session, [labels.LabelRow(record.id, "SC", 7, 1)])

    async def test_a_correction_appended_to_the_table_wins(self, session: AsyncSession) -> None:
        record = await make_record(session)
        await labels.apply_labels(
            session,
            [labels.LabelRow(record.id, "SC", 7, 1), labels.LabelRow(record.id, "CU", 3, 2)],
        )
        await session.refresh(record)
        assert (record.sky_class_label, record.observed_cloud_cover_oktas) == ("Cumulus", 3)


class TestReadingProbabilities:
    def test_long_form_is_one_row_per_class(self, tmp_path: Path) -> None:
        table = write(tmp_path, "p.csv", "tag,class,p\nWAS-V11-F1,CU,0.8\nWAS-V11-F1,SC,0.2\n")
        assert labels.parse_predictions(table) == [
            ("WAS-V11-F1", "CU", Decimal("0.80000")),
            ("WAS-V11-F1", "SC", Decimal("0.20000")),
        ]

    def test_wide_form_takes_its_classes_from_the_header(self, tmp_path: Path) -> None:
        table = write(tmp_path, "p.csv", "tag,CU,SC,CI\nWAS-V11-F1,0.8,0.15,0.05\n")
        assert labels.parse_predictions(table) == [
            ("WAS-V11-F1", "CU", Decimal("0.80000")),
            ("WAS-V11-F1", "SC", Decimal("0.15000")),
            ("WAS-V11-F1", "CI", Decimal("0.05000")),
        ]

    def test_a_percentage_is_read_as_one(self, tmp_path: Path) -> None:
        table = write(tmp_path, "p.csv", "tag,class,p\nWAS-V11-F1,CU,80%\n")
        assert labels.parse_predictions(table) == [("WAS-V11-F1", "CU", Decimal("0.80000"))]

    def test_a_bare_number_above_one_is_read_as_a_percentage(self, tmp_path: Path) -> None:
        # 80 is not a probability under any reading, and "80" is what a
        # notebook prints. Rejecting it would help nobody.
        table = write(tmp_path, "p.csv", "tag,class,p\nWAS-V11-F1,CU,80\n")
        assert labels.parse_predictions(table) == [("WAS-V11-F1", "CU", Decimal("0.80000"))]


class TestApplyingPredictions:
    async def test_the_whole_vector_is_stored(self, session: AsyncSession) -> None:
        record = await make_record(session)
        model = await labels.upsert_model(
            session, slug="allsky-cnn", name="All-sky CNN", version="1"
        )
        await labels.apply_predictions(
            session,
            [
                (record.id, "CU", Decimal("0.8")),
                (record.id, "SC", Decimal("0.15")),
                (record.id, "CI", Decimal("0.05")),
            ],
            model=model,
        )
        stored = (
            (
                await session.execute(
                    select(ImageClassPrediction).where(ImageClassPrediction.image_id == record.id)
                )
            )
            .scalars()
            .all()
        )
        assert {p.sky_class_label for p in stored} == {"Cumulus", "Stratocumulus", "Cirrus"}

    async def test_a_partial_vector_is_refused(self, session: AsyncSession) -> None:
        record = await make_record(session)
        model = await labels.upsert_model(session, slug="m", name="M", version="1")
        with pytest.raises(labels.LabelFormatError, match="sum to"):
            await labels.apply_predictions(
                session,
                [(record.id, "CU", Decimal("0.8")), (record.id, "SC", Decimal("0.1"))],
                model=model,
            )

    async def test_reloading_replaces_the_frame_rather_than_accumulating(
        self, session: AsyncSession
    ) -> None:
        # The second run scores two classes where the first scored three. The
        # dropped class must not survive as a stale row.
        record = await make_record(session)
        model = await labels.upsert_model(session, slug="m", name="M", version="1")
        await labels.apply_predictions(
            session,
            [
                (record.id, "CU", Decimal("0.5")),
                (record.id, "SC", Decimal("0.3")),
                (record.id, "CI", Decimal("0.2")),
            ],
            model=model,
        )
        await labels.apply_predictions(
            session,
            [(record.id, "CU", Decimal("0.6")), (record.id, "SC", Decimal("0.4"))],
            model=model,
        )
        stored = (
            (
                await session.execute(
                    select(ImageClassPrediction).where(ImageClassPrediction.image_id == record.id)
                )
            )
            .scalars()
            .all()
        )
        assert {p.sky_class_label for p in stored} == {"Cumulus", "Stratocumulus"}

    async def test_two_models_can_score_the_same_frame(self, session: AsyncSession) -> None:
        record = await make_record(session)
        first = await labels.upsert_model(session, slug="one", name="One", version="1")
        second = await labels.upsert_model(session, slug="two", name="Two", version="1")
        for model in (first, second):
            await labels.apply_predictions(
                session,
                [(record.id, "CU", Decimal("0.7")), (record.id, "SC", Decimal("0.3"))],
                model=model,
            )
        stored = (
            (
                await session.execute(
                    select(ImageClassPrediction).where(ImageClassPrediction.image_id == record.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(stored) == 4

    async def test_a_prediction_is_never_mistaken_for_an_observation(
        self, session: AsyncSession
    ) -> None:
        record = await make_record(session)
        model = await labels.upsert_model(session, slug="m", name="M", version="1")
        await labels.apply_predictions(
            session,
            [(record.id, "CU", Decimal("0.9")), (record.id, "SC", Decimal("0.1"))],
            model=model,
        )
        await session.refresh(record)
        assert record.sky_class_label is None
        assert record.observed_cloud_cover_oktas is None

    async def test_retraining_keeps_the_slug_and_moves_the_version(
        self, session: AsyncSession
    ) -> None:
        await labels.upsert_model(session, slug="m", name="M", version="1")
        again = await labels.upsert_model(session, slug="m", name="ignored", version="2")
        assert (again.name, again.version) == ("M", "2")
