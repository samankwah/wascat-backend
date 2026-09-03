"""Managing the controlled vocabularies.

The public API validates query parameters against these terms, so an edit here
changes what the archive will accept in a URL. These tests are about the three
consequences of that: a rename must not break an existing query, a merge must
move records rather than strand them, and a term the API depends on must not
be removable at all.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.errors import ConflictError, ForbiddenError
from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
)
from wascat.domains.vocab import service
from wascat.domains.vocab.models import VocabKind, VocabularyAlias, VocabularyTerm

pytestmark = pytest.mark.db

TEST_VIDEO = "vid9100"


async def make_term(
    session: AsyncSession,
    label: str,
    *,
    kind: VocabKind = VocabKind.LOCATION,
    system: bool = False,
) -> VocabularyTerm:
    term = VocabularyTerm(kind=kind, slug=service.slugify(label), label=label, system=system)
    session.add(term)
    await session.flush()
    return term


async def make_record_with(
    session: AsyncSession, term: VocabularyTerm, count: int = 1
) -> list[ImageRecord]:
    collection = Collection(slug=f"vid{uuid.uuid4().int % 100000}")
    session.add(collection)
    await session.flush()
    release = Release(collection_id=collection.id, version="1.0")
    session.add(release)
    await session.flush()

    records = []
    column = service.LABEL_COLUMNS[term.kind]
    id_column = service.ID_COLUMNS[term.kind]

    for _ in range(count):
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
            **{column: term.label, id_column: term.id},
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
        records.append(record)

    await session.commit()
    return records


class TestSlugify:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Kumasi", "kumasi"),
            ("Dry season", "dry-season"),
            ("Ouagadougou / Sahel", "ouagadougou-sahel"),
            ("  Spaced  Out  ", "spaced-out"),
            # Diacritics fold rather than vanish, so a French site name still
            # produces a usable key.
            ("Zinder Départ", "zinder-depart"),
        ],
    )
    def test_produces_a_stable_key(self, label: str, expected: str) -> None:
        assert service.slugify(label) == expected

    def test_refuses_a_label_with_no_usable_characters(self) -> None:
        with pytest.raises(ConflictError):
            service.slugify("!!!")


class TestUsageCounts:
    async def test_reports_how_many_records_carry_each_term(self, session: AsyncSession) -> None:
        term = await make_term(session, f"Kumasi {uuid.uuid4().hex[:6]}")
        await make_record_with(session, term, count=3)

        # The count is what makes the page actionable: it decides whether
        # removing a term is even a question.
        assert await service.count_usage(session, term) == 3

    async def test_an_unused_term_reports_zero(self, session: AsyncSession) -> None:
        term = await make_term(session, f"Unused {uuid.uuid4().hex[:6]}")
        await session.commit()
        assert await service.count_usage(session, term) == 0


class TestRenameKeepsExistingQueriesWorking:
    """The point of keeping the slug and recording an alias."""

    async def test_records_follow_the_new_label(self, session: AsyncSession) -> None:
        term = await make_term(session, f"Kumasi {uuid.uuid4().hex[:6]}")
        records = await make_record_with(session, term, count=2)
        new_label = f"Kumasi Airport {uuid.uuid4().hex[:6]}"

        await service.rename_term(session, term, label=new_label)
        await session.commit()

        for record in records:
            await session.refresh(record)
            # The filters read the denormalised label column, so a rename that
            # did not update it would leave the records unfindable.
            assert record.location_label == new_label

    async def test_the_old_value_still_resolves(self, session: AsyncSession) -> None:
        original = f"Kumasi {uuid.uuid4().hex[:6]}"
        term = await make_term(session, original)
        await session.commit()

        await service.rename_term(session, term, label=f"{original} Airport")
        await session.commit()

        # A bookmarked ?location=<old> - or one printed in a paper - keeps
        # working. This is the SKOS altLabel idea.
        found = await service.resolve(session, VocabKind.LOCATION, original)
        assert found is not None
        assert found.id == term.id

    async def test_the_slug_does_not_change(self, session: AsyncSession) -> None:
        term = await make_term(session, f"Kumasi {uuid.uuid4().hex[:6]}")
        slug = term.slug
        await session.commit()

        await service.rename_term(session, term, label="Something Else Entirely")
        await session.commit()

        assert term.slug == slug

    async def test_renaming_onto_an_existing_label_is_refused(self, session: AsyncSession) -> None:
        suffix = uuid.uuid4().hex[:6]
        first = await make_term(session, f"Kumasi {suffix}")
        second = await make_term(session, f"Tamale {suffix}")
        await session.commit()

        with pytest.raises(ConflictError, match="already exists"):
            await service.rename_term(session, second, label=first.label)


class TestMergeMovesRecordsRatherThanStrandingThem:
    async def test_every_record_moves_to_the_winner(self, session: AsyncSession) -> None:
        suffix = uuid.uuid4().hex[:6]
        loser = await make_term(session, f"Kumasi Old {suffix}")
        winner = await make_term(session, f"Kumasi {suffix}")
        records = await make_record_with(session, loser, count=3)

        moved = await service.merge_terms(session, loser=loser, winner=winner)
        await session.commit()

        assert moved == 3
        for record in records:
            await session.refresh(record)
            assert record.location_label == winner.label
            assert record.location_id == winner.id

    async def test_the_loser_is_retired_and_points_at_the_winner(
        self, session: AsyncSession
    ) -> None:
        suffix = uuid.uuid4().hex[:6]
        loser = await make_term(session, f"Kumasi Old {suffix}")
        winner = await make_term(session, f"Kumasi {suffix}")
        await session.commit()

        await service.merge_terms(session, loser=loser, winner=winner)
        await session.commit()

        # Retired, not deleted: the merge stays legible afterwards.
        assert loser.retired_at is not None
        assert loser.merged_into_id == winner.id

    async def test_the_old_value_now_resolves_to_the_winner(self, session: AsyncSession) -> None:
        suffix = uuid.uuid4().hex[:6]
        loser = await make_term(session, f"Kumasi Old {suffix}")
        winner = await make_term(session, f"Kumasi {suffix}")
        await session.commit()

        await service.merge_terms(session, loser=loser, winner=winner)
        await session.commit()

        found = await service.resolve(session, VocabKind.LOCATION, loser.label)
        assert found is not None
        assert found.id == winner.id

    async def test_a_term_cannot_be_merged_into_itself(self, session: AsyncSession) -> None:
        term = await make_term(session, f"Kumasi {uuid.uuid4().hex[:6]}")
        await session.commit()
        with pytest.raises(ConflictError, match="into itself"):
            await service.merge_terms(session, loser=term, winner=term)

    async def test_vocabularies_cannot_be_crossed(self, session: AsyncSession) -> None:
        suffix = uuid.uuid4().hex[:6]
        location = await make_term(session, f"Kumasi {suffix}")
        season = await make_term(session, f"Late dry {suffix}", kind=VocabKind.SKY_CLASS)
        await session.commit()

        with pytest.raises(ConflictError):
            await service.merge_terms(session, loser=location, winner=season)

    async def test_a_term_the_api_validates_against_cannot_be_merged_away(
        self, session: AsyncSession
    ) -> None:
        suffix = uuid.uuid4().hex[:6]
        system = await make_term(session, f"Harmattan {suffix}", system=True)
        other = await make_term(session, f"Dry {suffix}")
        await session.commit()

        with pytest.raises(ConflictError, match="public API validates"):
            await service.merge_terms(session, loser=system, winner=other)


class TestRetiring:
    async def test_an_unused_term_can_be_retired(self, session: AsyncSession) -> None:
        term = await make_term(session, f"Unused {uuid.uuid4().hex[:6]}")
        await session.commit()

        await service.retire_term(session, term)
        await session.commit()
        assert term.retired_at is not None

    async def test_a_term_in_use_cannot_be(self, session: AsyncSession) -> None:
        term = await make_term(session, f"Busy {uuid.uuid4().hex[:6]}")
        await make_record_with(session, term, count=4)

        with pytest.raises(ConflictError) as caught:
            await service.retire_term(session, term)

        # The message says what to do instead, rather than only refusing.
        assert "4 record" in str(caught.value)
        assert "Merge it into another term" in str(caught.value)

    async def test_a_term_the_api_validates_against_cannot_be(self, session: AsyncSession) -> None:
        term = await make_term(session, f"Harmattan {uuid.uuid4().hex[:6]}", system=True)
        await session.commit()

        with pytest.raises(ConflictError, match="narrow what the API accepts"):
            await service.retire_term(session, term)


class TestAddingToAFrozenVocabulary:
    async def test_is_refused_by_default(self, session: AsyncSession) -> None:
        # Adding a season widens what ?season= accepts, which is a contract
        # change rather than an editorial one.
        with pytest.raises(ForbiddenError, match="changes what the API will accept"):
            await service.create_term(session, kind=VocabKind.SEASON, label="Late Harmattan")

    async def test_an_open_vocabulary_accepts_new_terms(self, session: AsyncSession) -> None:
        # Locations are open-ended: the API reports what is present rather
        # than validating against a closed list.
        term = await service.create_term(
            session, kind=VocabKind.LOCATION, label=f"Tamale {uuid.uuid4().hex[:6]}"
        )
        await session.commit()
        assert term.slug.startswith("tamale-")

    async def test_a_duplicate_label_is_refused(self, session: AsyncSession) -> None:
        label = f"Kumasi {uuid.uuid4().hex[:6]}"
        await service.create_term(session, kind=VocabKind.LOCATION, label=label)
        await session.commit()

        with pytest.raises(ConflictError, match="Merge into it"):
            await service.create_term(session, kind=VocabKind.LOCATION, label=label)


class TestOrdering:
    async def test_terms_can_be_reordered(self, session: AsyncSession) -> None:
        suffix = uuid.uuid4().hex[:6]
        terms = [await make_term(session, f"Site {letter} {suffix}") for letter in "ABC"]
        await session.commit()

        await service.reorder(session, VocabKind.LOCATION, [terms[2].id, terms[0].id, terms[1].id])
        await session.commit()

        assert [term.position for term in terms] == [1, 2, 0]

    async def test_an_unknown_id_is_refused(self, session: AsyncSession) -> None:
        from wascat.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await service.reorder(session, VocabKind.LOCATION, [uuid.uuid4()])


class TestAliases:
    async def test_a_value_only_ever_resolves_to_one_term(self, session: AsyncSession) -> None:
        suffix = uuid.uuid4().hex[:6]
        original = f"Kumasi {suffix}"
        first = await make_term(session, original)
        await session.commit()

        # Rename away, so `original` becomes an alias of `first`.
        await service.rename_term(session, first, label=f"Kumasi Airport {suffix}")
        await session.commit()

        aliases = (
            (
                await session.execute(
                    select(VocabularyAlias).where(VocabularyAlias.value == original)
                )
            )
            .scalars()
            .all()
        )
        # Exactly one, pointing at exactly one term. Two would make a query
        # ambiguous.
        assert len(aliases) == 1
        assert aliases[0].term_id == first.id

    async def test_reusing_an_alias_as_a_new_label_is_refused(self, session: AsyncSession) -> None:
        suffix = uuid.uuid4().hex[:6]
        original = f"Kumasi {suffix}"
        term = await make_term(session, original)
        await session.commit()
        await service.rename_term(session, term, label=f"Kumasi Airport {suffix}")
        await session.commit()

        # The old label is an alias of the renamed term, so reusing it would
        # make a query ambiguous. Refused either as an alias clash or as a
        # duplicate, depending on which check sees it first - what matters is
        # that a second term cannot claim a value that already resolves.
        with pytest.raises(ConflictError):
            await service.create_term(session, kind=VocabKind.LOCATION, label=original)


class TestMeasurementsAreNeverTouched:
    async def test_a_merge_moves_classification_and_nothing_else(
        self, session: AsyncSession
    ) -> None:
        suffix = uuid.uuid4().hex[:6]
        loser = await make_term(session, f"Kumasi Old {suffix}")
        winner = await make_term(session, f"Kumasi {suffix}")
        records = await make_record_with(session, loser, count=1)

        record = records[0]
        record.cloud_fraction = Decimal("0.525588")
        record.cloud_cover_oktas = 4
        await session.commit()

        await service.merge_terms(session, loser=loser, winner=winner)
        await session.commit()
        await session.refresh(record)

        # A vocabulary edit is editorial. It must never disturb what the
        # pipeline measured.
        assert record.cloud_cover_oktas == 4
        assert record.cloud_fraction == Decimal("0.525588")
