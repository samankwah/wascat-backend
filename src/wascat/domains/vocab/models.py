"""Controlled vocabulary.

The facets that drive Explore's filters used to be frozen tuples in
lib/vocab.ts. Moving them into the database is what lets a curator add a sky
class or rename a location without a deploy - but it also means an edit can
now change the public API's accepted values, so two safeguards exist:

  * terms seeded as ``system`` cannot be deleted, because the public query
    schema validates against them;
  * renaming keeps the slug and records the old label as an alias, so a
    bookmarked ``?season=Harmattan`` keeps resolving after the rename.

Merging never deletes: the losing term is retired, pointed at the winner, and
its label kept as an alias. Nothing that was ever a valid query value stops
being one.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, UniqueConstraint, false, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wascat.core.db import Base, ts_created, ts_updated, uuid_pk


class VocabKind(StrEnum):
    """The vocabularies a record can be classified against."""

    SEASON = "season"
    TIME_OF_DAY = "time_of_day"
    LOCATION = "location"
    SKY_CLASS = "sky_class"
    CONDITION_TAG = "condition_tag"


#: Kinds whose values the public query schema validates as a closed enum.
#: Adding a term to one of these widens the public contract, so the service
#: refuses unless explicitly allowed.
CONTRACT_KINDS = frozenset({VocabKind.SEASON, VocabKind.TIME_OF_DAY})

#: Seeded on first migration, matching lib/vocab.ts exactly.
SYSTEM_TERMS: dict[VocabKind, tuple[str, ...]] = {
    VocabKind.SEASON: ("Harmattan", "Dry season", "Wet season", "Transition"),
    VocabKind.TIME_OF_DAY: ("Morning", "Midday", "Afternoon", "Evening"),
}


class VocabularyTerm(Base):
    __tablename__ = "vocabulary_terms"

    id: Mapped[uuid_pk]
    kind: Mapped[VocabKind] = mapped_column(Enum(VocabKind, name="vocab_kind", native_enum=True))
    slug: Mapped[str]
    label: Mapped[str]
    description: Mapped[str | None]
    position: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # System terms back the public API's enum validation and cannot be deleted.
    system: Mapped[bool] = mapped_column(default=False, server_default=false())

    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vocabulary_terms.id", ondelete="SET NULL")
    )
    retired_at: Mapped[datetime | None]

    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]

    aliases: Mapped[list[VocabularyAlias]] = relationship(
        back_populates="term", cascade="all, delete-orphan"
    )
    merged_into: Mapped[VocabularyTerm | None] = relationship(remote_side="VocabularyTerm.id")

    __table_args__ = (
        UniqueConstraint("kind", "slug"),
        UniqueConstraint("kind", "label"),
        CheckConstraint(r"slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'", name="slug_format"),
        CheckConstraint("merged_into_id IS NULL OR merged_into_id <> id", name="no_self_merge"),
        CheckConstraint("NOT system OR retired_at IS NULL", name="system_terms_stay_active"),
        Index("ix_vocabulary_terms_kind_position", "kind", "position"),
        Index(
            "ix_vocabulary_terms_kind_active",
            "kind",
            postgresql_where=text("retired_at IS NULL"),
        ),
    )


class VocabularyAlias(Base):
    """A value that still resolves to a term after a rename or a merge."""

    __tablename__ = "vocabulary_aliases"

    id: Mapped[uuid_pk]
    term_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vocabulary_terms.id", ondelete="CASCADE")
    )
    kind: Mapped[VocabKind] = mapped_column(Enum(VocabKind, name="vocab_kind", native_enum=True))
    value: Mapped[str]
    created_at: Mapped[ts_created]

    term: Mapped[VocabularyTerm] = relationship(back_populates="aliases")

    __table_args__ = (
        UniqueConstraint("kind", "value"),
        Index("ix_vocabulary_aliases_term_id", "term_id"),
    )
