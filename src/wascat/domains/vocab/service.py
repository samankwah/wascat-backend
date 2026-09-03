"""Managing the controlled vocabularies.

These terms are not labels on a page: the public API validates query
parameters against them, so editing one changes what the archive will accept
in a URL. Three rules follow from that, and they are what this module is
mostly about.

**Nothing is ever deleted while it is in use.** A term with records behind it
can be merged into another or retired, never dropped, because a term that
vanishes takes its records' classification with it.

**A rename keeps the old value working.** The label changes and the slug does
not, and the old label is kept as an alias - so a bookmarked
``?season=Harmattan`` still resolves after someone renames it to "Harmattan
season". This is the SKOS ``altLabel`` idea and the reason WordPress keeps
term slugs stable across renames.

**A merge is a re-pointing, not a deletion.** Every record moves from the
losing term to the winner in one transaction, and the loser is retired with a
pointer to where it went, so the history stays readable.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.config import get_settings
from wascat.core.errors import ConflictError, ForbiddenError, NotFoundError
from wascat.domains.catalog.models import ImageRecord
from wascat.domains.vocab.models import (
    CONTRACT_KINDS,
    VocabKind,
    VocabularyAlias,
    VocabularyTerm,
)

#: Which record column each vocabulary classifies. The denormalised label
#: columns are what the filters read, so a rename has to update them too.
LABEL_COLUMNS: dict[VocabKind, str] = {
    VocabKind.SEASON: "season_label",
    VocabKind.TIME_OF_DAY: "time_of_day_label",
    VocabKind.LOCATION: "location_label",
    VocabKind.SKY_CLASS: "sky_class_label",
}

ID_COLUMNS: dict[VocabKind, str] = {
    VocabKind.SEASON: "season_id",
    VocabKind.TIME_OF_DAY: "time_of_day_id",
    VocabKind.LOCATION: "location_id",
    VocabKind.SKY_CLASS: "sky_class_id",
}


@dataclass(frozen=True, slots=True)
class TermUsage:
    term: VocabularyTerm
    #: How many records currently carry this term. The number that decides
    #: whether deleting is even a question.
    records: int


def slugify(label: str) -> str:
    """A stable, URL-safe key for a label.

    Kept when the label is renamed, which is what lets an old query value keep
    resolving.
    """
    normalised = unicodedata.normalize("NFKD", label)
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    if not slug:
        raise ConflictError(f"{label!r} does not produce a usable identifier.")
    return slug


async def list_terms(session: AsyncSession, kind: VocabKind) -> list[TermUsage]:
    """Every term of one kind, with the number of records using it.

    The count is what makes the page actionable: a term with no records can be
    removed freely, and one with four hundred cannot be removed at all.
    """
    label_column = getattr(ImageRecord, LABEL_COLUMNS[kind])

    counts: dict[str | None, int] = {
        label: count
        for label, count in (
            await session.execute(
                select(label_column, func.count(ImageRecord.id))
                .where(ImageRecord.retired_at.is_(None))
                .group_by(label_column)
            )
        ).all()
    }

    terms = (
        (
            await session.execute(
                select(VocabularyTerm)
                .where(VocabularyTerm.kind == kind)
                .order_by(VocabularyTerm.position, VocabularyTerm.label)
            )
        )
        .scalars()
        .all()
    )

    return [TermUsage(term=term, records=counts.get(term.label, 0)) for term in terms]


async def get_term(session: AsyncSession, term_id: uuid.UUID) -> VocabularyTerm:
    term = await session.get(VocabularyTerm, term_id)
    if term is None:
        raise NotFoundError("Vocabulary term not found")
    return term


async def count_usage(session: AsyncSession, term: VocabularyTerm) -> int:
    label_column = getattr(ImageRecord, LABEL_COLUMNS[term.kind])
    return (
        await session.execute(
            select(func.count(ImageRecord.id)).where(
                label_column == term.label, ImageRecord.retired_at.is_(None)
            )
        )
    ).scalar_one()


async def create_term(
    session: AsyncSession,
    *,
    kind: VocabKind,
    label: str,
    description: str | None = None,
) -> VocabularyTerm:
    """Add a term.

    Adding to a vocabulary the public API validates as a closed set widens
    what the API accepts, which is a contract change rather than an editorial
    one. It is refused unless explicitly allowed, because the alternative is
    an API whose accepted values drift silently.
    """
    settings = get_settings()
    if kind in CONTRACT_KINDS and not settings.allow_vocab_expansion:
        raise ForbiddenError(
            f"The {kind.value.replace('_', ' ')} vocabulary is part of the public "
            "API's accepted values, so adding to it changes what the API will "
            "accept. Set WASCAT_ALLOW_VOCAB_EXPANSION to allow it deliberately.",
            code="vocab_frozen",
        )

    label = label.strip()
    slug = slugify(label)
    await _assert_available(session, kind=kind, slug=slug, label=label)

    position = (
        await session.execute(
            select(func.coalesce(func.max(VocabularyTerm.position), -1) + 1).where(
                VocabularyTerm.kind == kind
            )
        )
    ).scalar_one()

    term = VocabularyTerm(
        kind=kind, slug=slug, label=label, description=description, position=position
    )
    session.add(term)
    await session.flush()
    return term


async def rename_term(
    session: AsyncSession, term: VocabularyTerm, *, label: str, description: str | None = None
) -> VocabularyTerm:
    """Change a term's label, keeping every existing reference working.

    The slug does not change and the old label becomes an alias, so a URL or a
    saved query using the previous value still resolves. Records carrying the
    old label are updated in the same transaction, because the filters read
    those denormalised columns rather than joining.
    """
    label = label.strip()
    previous = term.label
    if label == previous:
        if description is not None:
            term.description = description
            await session.flush()
        return term

    await _assert_available(session, kind=term.kind, label=label, exclude=term.id)

    term.label = label
    if description is not None:
        term.description = description

    # The old value keeps resolving. This is what stops a rename from silently
    # breaking somebody's bookmark or a published query in a paper.
    await _record_alias(session, term, previous)

    label_column = getattr(ImageRecord, LABEL_COLUMNS[term.kind])
    await session.execute(
        update(ImageRecord).where(label_column == previous).values({label_column: label})
    )
    await session.flush()
    return term


async def merge_terms(
    session: AsyncSession, *, loser: VocabularyTerm, winner: VocabularyTerm
) -> int:
    """Fold one term into another, and report how many records moved.

    Nothing is deleted. Every record is re-pointed, the loser is retired with
    a pointer to the winner, and the loser's label and slug become aliases -
    so the merge is legible afterwards and old queries still resolve.
    """
    if loser.id == winner.id:
        raise ConflictError("A term cannot be merged into itself.")
    if loser.kind is not winner.kind:
        raise ConflictError(
            f"A {loser.kind.value.replace('_', ' ')} cannot be merged into a "
            f"{winner.kind.value.replace('_', ' ')}."
        )
    if loser.system:
        raise ConflictError(
            f"{loser.label!r} is one of the values the public API validates "
            "against, so it cannot be merged away."
        )

    id_column = getattr(ImageRecord, ID_COLUMNS[loser.kind])
    label_column = getattr(ImageRecord, LABEL_COLUMNS[loser.kind])

    result = await session.execute(
        update(ImageRecord)
        .where((id_column == loser.id) | (label_column == loser.label))
        .values({id_column: winner.id, label_column: winner.label})
    )
    moved = result.rowcount if hasattr(result, "rowcount") else 0

    loser.merged_into_id = winner.id
    loser.retired_at = datetime.now(UTC)

    # Both of the loser's identifiers keep resolving, now pointing at the
    # winner.
    await _record_alias(session, winner, loser.label)
    await _record_alias(session, winner, loser.slug)

    await session.flush()
    return moved or 0


async def retire_term(session: AsyncSession, term: VocabularyTerm) -> None:
    """Take a term out of use.

    Refused while records still carry it, and refused outright for the terms
    the public API validates against. Retiring is not deletion: the row stays,
    so the audit trail and any alias pointing at it keep working.
    """
    if term.system:
        raise ConflictError(
            f"{term.label!r} is one of the values the public API validates "
            "against. Removing it would narrow what the API accepts."
        )

    usage = await count_usage(session, term)
    if usage:
        raise ConflictError(
            f"{usage:,} record{'s' if usage != 1 else ''} still "
            f"{'use' if usage != 1 else 'uses'} {term.label!r}. Merge it into "
            "another term instead, which moves them rather than stranding them."
        )

    term.retired_at = datetime.now(UTC)
    await session.flush()


async def restore_term(session: AsyncSession, term: VocabularyTerm) -> None:
    term.retired_at = None
    term.merged_into_id = None
    await session.flush()


async def reorder(session: AsyncSession, kind: VocabKind, ordered_ids: list[uuid.UUID]) -> None:
    """Set the order terms appear in.

    Position is a plain integer rather than a link in a list, so a partial or
    duplicated ordering cannot corrupt the sequence - the worst case is that
    something sorts in an unexpected place.
    """
    terms = {
        term.id: term
        for term in (
            await session.execute(select(VocabularyTerm).where(VocabularyTerm.kind == kind))
        )
        .scalars()
        .all()
    }

    unknown = [str(term_id) for term_id in ordered_ids if term_id not in terms]
    if unknown:
        raise NotFoundError(f"No such {kind.value} term: {', '.join(unknown)}")

    for position, term_id in enumerate(ordered_ids):
        terms[term_id].position = position
    await session.flush()


async def resolve(session: AsyncSession, kind: VocabKind, value: str) -> VocabularyTerm | None:
    """Find a term by label, slug, or a value it used to be known by.

    The alias step is what makes a rename safe: a query written against the
    old label still finds the term it was written for.
    """
    value = value.strip()

    # Active terms first. A merged term keeps its own label until something
    # else claims it, so matching any term would resolve an old query to the
    # retired loser rather than following it to the winner - which is the one
    # thing the merge exists to prevent.
    term = (
        (
            await session.execute(
                select(VocabularyTerm).where(
                    VocabularyTerm.kind == kind,
                    VocabularyTerm.retired_at.is_(None),
                    (VocabularyTerm.label == value) | (VocabularyTerm.slug == value),
                )
            )
        )
        .scalars()
        .first()
    )
    if term is not None:
        return term

    alias = (
        (
            await session.execute(
                select(VocabularyAlias).where(
                    VocabularyAlias.kind == kind, VocabularyAlias.value == value
                )
            )
        )
        .scalars()
        .first()
    )
    if alias is None:
        # Last resort: a retired term still answers to its own name, so an
        # old query at least finds where it went rather than nothing at all.
        return await _follow_merges(
            session,
            (
                (
                    await session.execute(
                        select(VocabularyTerm).where(
                            VocabularyTerm.kind == kind,
                            (VocabularyTerm.label == value) | (VocabularyTerm.slug == value),
                        )
                    )
                )
                .scalars()
                .first()
            ),
        )
    return await _follow_merges(session, await session.get(VocabularyTerm, alias.term_id))


async def _follow_merges(
    session: AsyncSession, term: VocabularyTerm | None, depth: int = 8
) -> VocabularyTerm | None:
    """Follow a chain of merges to the term still in use.

    A term merged into one that was later merged again should still resolve to
    wherever its records actually are. The depth cap guards against a cycle
    that should not exist but would otherwise hang the request.
    """
    seen: set[uuid.UUID] = set()
    while term is not None and term.merged_into_id is not None and depth > 0:
        if term.id in seen:
            break
        seen.add(term.id)
        term = await session.get(VocabularyTerm, term.merged_into_id)
        depth -= 1
    return term


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _assert_available(
    session: AsyncSession,
    *,
    kind: VocabKind,
    label: str,
    slug: str | None = None,
    exclude: uuid.UUID | None = None,
) -> None:
    # Built from the checks that actually apply. A rename supplies no slug,
    # because the slug is deliberately kept - so there is nothing to compare
    # it against, and inventing a sentinel to compare would be both obscure
    # and, with a NUL byte, rejected by PostgreSQL outright.
    matches = VocabularyTerm.label == label
    if slug is not None:
        matches = matches | (VocabularyTerm.slug == slug)

    clash = select(VocabularyTerm).where(VocabularyTerm.kind == kind, matches)
    if exclude is not None:
        clash = clash.where(VocabularyTerm.id != exclude)

    existing = (await session.execute(clash)).scalars().first()
    if existing is not None:
        raise ConflictError(
            f"{existing.label!r} already exists in this vocabulary. Merge into it "
            "rather than creating a second term for the same thing."
        )

    # An alias pointing somewhere else would make the new label ambiguous.
    alias = (
        (
            await session.execute(
                select(VocabularyAlias).where(
                    VocabularyAlias.kind == kind, VocabularyAlias.value == label
                )
            )
        )
        .scalars()
        .first()
    )
    if alias is not None and alias.term_id != exclude:
        raise ConflictError(
            f"{label!r} is already an alias for another term, so reusing it would "
            "make queries ambiguous."
        )


async def _record_alias(session: AsyncSession, term: VocabularyTerm, value: str) -> None:
    """Keep an old value resolving, without ever duplicating one."""
    if value == term.label or value == term.slug:
        return

    existing = (
        (
            await session.execute(
                select(VocabularyAlias).where(
                    VocabularyAlias.kind == term.kind, VocabularyAlias.value == value
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        # Re-point rather than duplicate: a value resolves to exactly one term.
        existing.term_id = term.id
        return

    session.add(VocabularyAlias(term_id=term.id, kind=term.kind, value=value))
