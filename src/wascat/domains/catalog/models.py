"""Catalogue tables: Collection -> Release -> ImageRecord -> Artifact.

Carried over from the prisma/schema.prisma the original repo declared but
never wired up, extended with the columns the admin dashboard needs and the
denormalised columns the public API reads on its hot path.

Several of the domain invariants are enforced here as CHECK constraints and
triggers rather than only in the service layer. That is deliberate: the
service layer produces good error messages, but the database is what makes
the rules *true* regardless of which code path writes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wascat.core.db import Base, ts_created, ts_updated, uuid_pk

if TYPE_CHECKING:
    from wascat.domains.vocab.models import VocabularyTerm

# Artifact types that represent the frame itself, and so must match the
# record's dimensions. Derivatives (thumbnail, webp) are deliberately exempt:
# they are additive, generated at ingest, and never appear in the public
# `artifacts[]` array. See risk R12.
PRIMARY_ARTIFACT_TYPES = ("source", "mask")
DERIVATIVE_ARTIFACT_TYPES = ("thumbnail", "webp")

# A capture sequence is "seq-" plus a three-digit ordinal, optionally preceded
# by the capture date: "seq-001", or "seq-20260904-001" once the capture team
# supplies timestamps. Both forms are fixed width, so they sort byte-wise, and
# '0' < '2' puts the undated eleven ahead of anything dated - the same
# discriminator sort_key uses to separate ISO timestamps from frame positions.
SEQUENCE_ID_PATTERN = r"^seq-(?:\d{8}-)?\d{3}$"
SEQUENCE_ID_SQL_REGEX = r"sequence_id ~ '^seq-(\d{8}-)?\d{3}$'"


class ReleaseStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[uuid_pk]
    slug: Mapped[str] = mapped_column(unique=True)
    title: Mapped[str | None]
    description: Mapped[str | None]

    # Editorial metadata. Every one of these is NULL for all 11 sequences
    # today, which is exactly why the admin dashboard exists.
    location_name: Mapped[str | None]
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    instrument: Mapped[str | None]
    license: Mapped[str | None]
    citation: Mapped[str | None]
    doi: Mapped[str | None]
    methods_url: Mapped[str | None]
    publication_url: Mapped[str | None]

    position: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]

    releases: Mapped[list[Release]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        order_by="Release.version",
    )

    __table_args__ = (
        CheckConstraint(
            r"slug ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'",
            name="slug_format",
        ),
        Index("ix_collections_position", "position"),
    )


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[uuid_pk]
    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE")
    )
    version: Mapped[str]
    status: Mapped[ReleaseStatus] = mapped_column(
        Enum(ReleaseStatus, name="release_status", native_enum=True),
        default=ReleaseStatus.DRAFT,
        server_default=text("'DRAFT'"),
    )
    current: Mapped[bool] = mapped_column(default=False, server_default=false())

    published_at: Mapped[datetime | None]
    retired_at: Mapped[datetime | None]
    capture_start: Mapped[datetime | None]
    capture_end: Mapped[datetime | None]

    bundle_url: Mapped[str | None]
    bundle_checksum: Mapped[str | None]
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", nullable=True)

    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]

    collection: Mapped[Collection] = relationship(back_populates="releases")
    images: Mapped[list[ImageRecord]] = relationship(
        back_populates="release", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("collection_id", "version"),
        # At most one current release per collection. A partial unique index
        # expresses "single winner" without a trigger.
        Index(
            "uq_releases_collection_current",
            "collection_id",
            unique=True,
            postgresql_where=text("current"),
        ),
        Index("ix_releases_collection_id_current", "collection_id", "current"),
        CheckConstraint(r"version ~ '^\d+\.\d+(?:\.\d+)?$'", name="version_format"),
        CheckConstraint(
            "status <> 'PUBLISHED' OR published_at IS NOT NULL",
            name="published_has_timestamp",
        ),
        CheckConstraint("NOT current OR status = 'PUBLISHED'", name="current_is_published"),
    )


class ImageRecord(Base):
    __tablename__ = "image_records"

    # The natural key from the delivery pipeline, e.g. "WAS-V01-F5".
    id: Mapped[str] = mapped_column(primary_key=True)
    release_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("releases.id", ondelete="CASCADE"))
    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="RESTRICT")
    )

    # The capture run this frame belongs to, e.g. "seq-001". Zero-padded so it
    # orders byte-wise, and fixed width so "seq-001" is never a prefix of
    # "seq-011" - which is what the substring search used to trip over.
    sequence_id: Mapped[str]
    # The trailing ordinal, parsed from sequence_id by trigger. Still carried
    # because sort_key needs it as an integer; ordering reads sequence_id.
    sequence_number: Mapped[int] = mapped_column(SmallInteger)
    frame_index: Mapped[int]

    # Measurement. Both NULL together or both set together; never invented.
    cloud_fraction: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    cloud_cover_oktas: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # The observer's total cloud cover, read off the sky by a person and
    # supplied in the label table alongside the cloud genus. It is a separate
    # column rather than a source for cloud_cover_oktas above: that one is
    # derived from the segmentation mask and pinned to cloud_fraction by
    # `okta_formula`, so writing a human count into it would make the archive
    # claim a measurement it never made. Where the two disagree, both are
    # served and the disagreement is the interesting part.
    observed_cloud_cover_oktas: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Scale at which the mask was delivered relative to its source frame. 1 for
    # correctly registered sequences; >1 where the mask was rendered larger, in
    # which case the viewer scales it back. Masks are stored as delivered.
    mask_scale: Mapped[Decimal] = mapped_column(
        Numeric(9, 6), default=Decimal(1), server_default=text("1")
    )

    width: Mapped[int]
    height: Mapped[int]

    # Provenance supplied per sequence by the capture team. All NULL today.
    captured_at: Mapped[datetime | None]
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)
    instrument: Mapped[str | None]

    location_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vocabulary_terms.id", ondelete="SET NULL")
    )
    season_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vocabulary_terms.id", ondelete="SET NULL")
    )
    time_of_day_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vocabulary_terms.id", ondelete="SET NULL")
    )
    sky_class_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vocabulary_terms.id", ondelete="SET NULL")
    )
    condition_tags: Mapped[list[str]] = mapped_column(default=list, server_default=text("'{}'"))

    # Denormalised label copies, kept in step by the vocabulary rename/merge
    # service. They let the derive trigger stay a single-row operation and let
    # the hot read path skip four joins.
    location_label: Mapped[str | None]
    season_label: Mapped[str | None]
    time_of_day_label: Mapped[str | None]
    sky_class_label: Mapped[str | None]

    provenance: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    custom: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)

    # Maintained by trigger from the artifact rows. Never written by hand.
    has_source: Mapped[bool] = mapped_column(default=False, server_default=false())
    has_mask: Mapped[bool] = mapped_column(default=False, server_default=false())

    # Derived by trigger. COLLATE "C" makes ordering byte-wise and identical
    # across platforms and Postgres locales; see risk R6.
    sort_key: Mapped[str] = mapped_column(Text(collation="C"))
    search_text: Mapped[str] = mapped_column(server_default=text("''"))
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', search_text)", persisted=True),
        nullable=True,
    )

    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]
    retired_at: Mapped[datetime | None]

    release: Mapped[Release] = relationship(back_populates="images")
    collection: Mapped[Collection] = relationship()
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Artifact.type",
    )
    location_term: Mapped[VocabularyTerm | None] = relationship(foreign_keys=[location_id])
    season_term: Mapped[VocabularyTerm | None] = relationship(foreign_keys=[season_id])
    time_of_day_term: Mapped[VocabularyTerm | None] = relationship(foreign_keys=[time_of_day_id])
    sky_class_term: Mapped[VocabularyTerm | None] = relationship(foreign_keys=[sky_class_id])
    # Not eager: the list endpoint never renders a probability vector, and
    # loading eleven classes per card would multiply every Explore page by
    # eleven. The single-record read asks for them explicitly.
    predictions: Mapped[list[ImageClassPrediction]] = relationship(
        back_populates="image",
        cascade="all, delete-orphan",
        order_by="(ImageClassPrediction.model_id, ImageClassPrediction.probability.desc())",
    )

    __table_args__ = (
        # -- Domain invariants, enforced by the database -------------------
        # 4: a fraction and its okta bucket are present together or not at all.
        CheckConstraint(
            "(cloud_fraction IS NULL) = (cloud_cover_oktas IS NULL)",
            name="measurement_pairing",
        ),
        # Invariant 3 (cloud cover requires a mask) is enforced by the same
        # deferred constraint trigger as invariant 2, not by a CHECK. Both
        # depend on has_mask, which is derived from the artifact rows written
        # after the record, so an immediate check would reject every measured
        # record at insert time.
        # Invariant 2 (a record holds a source, a mask, or both, never
        # neither) is NOT a CHECK constraint: PostgreSQL cannot defer one, and
        # an immediate check would reject every insert, since the record row is
        # written before its artifacts. It is a deferred constraint trigger,
        # created in the triggers migration.
        # 11: the okta bucket is derived, not free-form.
        CheckConstraint(
            "cloud_cover_oktas IS NULL"
            " OR cloud_cover_oktas = least(8, greatest(0, round(cloud_fraction * 8)::int))",
            name="okta_formula",
        ),
        # The observed count is free of that formula - it is not derived from
        # anything - but it is still a synoptic okta and so still eighths.
        CheckConstraint(
            "observed_cloud_cover_oktas IS NULL OR observed_cloud_cover_oktas BETWEEN 0 AND 8",
            name="observed_okta_range",
        ),
        # 10: identifier formats.
        CheckConstraint(r"id ~ '^WAS-[A-Z0-9-]+$'", name="id_format"),
        CheckConstraint(SEQUENCE_ID_SQL_REGEX, name="sequence_id_format"),
        # A dated sequence id repeats its ordinal across days, so two of them
        # would share a numeric sort_key prefix. That never happens because a
        # dated sequence sorts by its timestamp instead - which is only true if
        # the timestamp is actually there. Make the database say so.
        CheckConstraint(
            r"sequence_id !~ '^seq-\d{8}-' OR captured_at IS NOT NULL",
            name="dated_sequence_has_timestamp",
        ),
        CheckConstraint("width > 0 AND height > 0", name="positive_dimensions"),
        CheckConstraint("frame_index >= 0", name="frame_index_non_negative"),
        CheckConstraint("mask_scale >= 1", name="mask_scale_at_least_one"),
        # -- Read paths -----------------------------------------------------
        Index("ix_image_records_sort", "sort_key", "id"),
        Index("ix_image_records_release_sort", "release_id", "sort_key", "id"),
        Index("ix_image_records_collection_sort", "collection_id", "sort_key", "id"),
        Index("ix_image_records_sequence_sort", "sequence_id", "sort_key", "id"),
        Index("ix_image_records_segmented_sort", "has_mask", "sort_key", "id"),
        # A frame is unique *within a release*, not across the archive: a
        # later release legitimately re-ingests seq-001 frame 5, whether
        # re-measured or re-encoded. Making this global would make the
        # second release of any sequence impossible to create.
        Index(
            "ix_image_records_release_frame",
            "release_id",
            "sequence_id",
            "frame_index",
            unique=True,
        ),
        # Invariants 7 and 8 as physical indexes: a record with no measurement
        # or no timestamp is simply not in the index the filter uses, so it
        # cannot be returned by a cloud-cover or date query by accident.
        Index(
            "ix_image_records_oktas_sort",
            "cloud_cover_oktas",
            "sort_key",
            "id",
            postgresql_where=text("cloud_cover_oktas IS NOT NULL"),
        ),
        Index(
            "ix_image_records_captured_sort",
            "captured_at",
            "sort_key",
            "id",
            postgresql_where=text("captured_at IS NOT NULL"),
        ),
        Index("ix_image_records_taxonomy", "sky_class_id", "season_id", "time_of_day_id"),
        Index("ix_image_records_location", "location_id"),
        Index("ix_image_records_tags", "condition_tags", postgresql_using="gin"),
        Index("ix_image_records_search_tsv", "search_tsv", postgresql_using="gin"),
        # Substring search parity: lib/search.ts used String.includes, so the
        # default `q` stays a LIKE '%...%' and needs a trigram index to be fast.
        Index(
            "ix_image_records_search_trgm",
            text("search_text gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid_pk]
    image_id: Mapped[str] = mapped_column(ForeignKey("image_records.id", ondelete="CASCADE"))
    type: Mapped[str]
    media_type: Mapped[str]
    object_key: Mapped[str] = mapped_column(unique=True)
    checksum: Mapped[str]
    bytes: Mapped[int] = mapped_column(BigInteger)
    width: Mapped[int | None]
    height: Mapped[int | None]
    meta: Mapped[dict[str, Any] | None] = mapped_column("metadata", nullable=True)
    created_at: Mapped[ts_created]

    image: Mapped[ImageRecord] = relationship(back_populates="artifacts")

    __table_args__ = (
        # 6: one artifact of each type per record.
        UniqueConstraint("image_id", "type"),
        # 10: SHA-256, lowercase hex.
        CheckConstraint(r"checksum ~ '^[a-f0-9]{64}$'", name="checksum_sha256"),
        CheckConstraint("bytes > 0", name="bytes_positive"),
        CheckConstraint(
            "type IN ('source', 'mask', 'thumbnail', 'webp')",
            name="known_type",
        ),
        Index("ix_artifacts_type", "type"),
        Index("ix_artifacts_image_id", "image_id"),
    )

    # NOTE: public_url is intentionally not a column. It is derived at read
    # time as f"{PUBLIC_ASSET_BASE_URL}/{object_key}", so moving the archive
    # behind a CDN is a config change rather than a data migration. With an
    # empty base it renders "/frames/seq-001/2-source.jpg", the same path the
    # site serves and the seed uploads to.


class PredictionModel(Base):
    """A classifier whose output the archive stores.

    The archive does not run inference - it records what a model said, the
    same way it records what the segmentation pipeline delivered. Keeping the
    model as a row rather than a string means two models can score the same
    frame and be compared, and means a model's identity survives being
    retrained: a new weights version is a new row, not an edit.
    """

    __tablename__ = "prediction_models"

    id: Mapped[uuid_pk]
    slug: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    # Free-form because the archive does not own the model's release scheme:
    # a git sha, a date and "v2" are all legitimate here.
    version: Mapped[str]
    description: Mapped[str | None]
    trained_at: Mapped[datetime | None]
    position: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]
    retired_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint(r"slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="model_slug_format"),
        Index("ix_prediction_models_position", "position", "slug"),
    )


class ImageClassPrediction(Base):
    """One class's probability, for one frame, from one model.

    Stored as the whole vector rather than a winning label: a real sky holds
    several genera at once, so "Cumulus" alone throws away most of what the
    model said. One row per class keeps the vector queryable - "every frame
    where Cumulonimbus scored above 0.3" is an index scan, not a JSON scan.
    """

    __tablename__ = "image_class_predictions"

    id: Mapped[uuid_pk]
    image_id: Mapped[str] = mapped_column(ForeignKey("image_records.id", ondelete="CASCADE"))
    model_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prediction_models.id", ondelete="CASCADE")
    )
    sky_class_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("vocabulary_terms.id", ondelete="SET NULL")
    )
    # Denormalised alongside the FK, the same bargain ImageRecord.sky_class_label
    # strikes: the vocabulary service keeps it in step through rename and merge,
    # and the read path skips a join per class per frame.
    sky_class_label: Mapped[str]
    # Five decimal places: enough to keep a softmax tail distinguishable from
    # zero, exact rather than float so a stored vector still sums to 1.
    probability: Mapped[Decimal] = mapped_column(Numeric(6, 5))

    created_at: Mapped[ts_created]

    image: Mapped[ImageRecord] = relationship(back_populates="predictions")
    model: Mapped[PredictionModel] = relationship()
    sky_class_term: Mapped[VocabularyTerm | None] = relationship(foreign_keys=[sky_class_id])

    __table_args__ = (
        # One score per class per model per frame. Re-ingesting a model's
        # output updates in place rather than accumulating duplicates.
        UniqueConstraint("image_id", "model_id", "sky_class_label", name="uq_prediction_class"),
        CheckConstraint("probability >= 0 AND probability <= 1", name="probability_range"),
        # The read path is always "this frame, this model, ranked".
        Index("ix_predictions_image_model", "image_id", "model_id", "probability"),
        # And the cross-archive one: "where did this class score high".
        Index("ix_predictions_class_probability", "model_id", "sky_class_label", "probability"),
    )
