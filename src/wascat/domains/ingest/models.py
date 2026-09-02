"""Ingest bookkeeping.

Two tables, both of which exist to make the pipeline reproducible rather than
merely runnable.

``SequenceReference`` pins the frame each sequence takes its reference circle
from. The TypeScript pipeline used ``Object.keys(pairedByVideo)[0]``, which is
readdir order - so it depended on the filesystem, and a Linux CI run could
legitimately choose a different frame and produce different geometry from the
same inputs. Pinning it makes the measurement a property of the data instead
of a property of the machine. See risk R16.

``IngestRun`` records what an ingest did, so a number in the catalogue can
always be traced back to the run that produced it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from wascat.core.db import Base, ts_created, ts_updated, uuid_pk


class IngestStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    # Set on startup for anything left RUNNING when the process died, so a
    # crashed run is never mistaken for one still in progress.
    INTERRUPTED = "INTERRUPTED"


class SequenceReference(Base):
    """The pinned reference frame and registration sample for one sequence."""

    __tablename__ = "sequence_references"

    id: Mapped[uuid_pk]
    video_id: Mapped[str] = mapped_column(unique=True)

    # Frame whose source image defines the valid circular field of view. Used
    # directly for its own sequence, and reused for mask-only records that have
    # no source frame of their own to measure.
    reference_frame_index: Mapped[int]
    valid_circle_pixels: Mapped[int | None]

    # Frames sampled when deciding whether the masks are registered. Pinned for
    # the same reason as the reference frame.
    registration_sample: Mapped[list[str]] = mapped_column(
        default=list, server_default=text("'{}'")
    )

    # Outcome, mirrored into every record of the sequence as `mask_scale`.
    mask_scale: Mapped[Decimal] = mapped_column(
        Numeric(9, 6), default=Decimal(1), server_default=text("1")
    )
    corrected: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    cloud_outside_field_of_view: Mapped[Decimal | None] = mapped_column(
        Numeric(9, 6), nullable=True
    )

    geometry: Mapped[str | None]
    frames: Mapped[int | None]

    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]

    __table_args__ = (
        CheckConstraint(r"video_id ~ '^vid[0-9]+$'", name="video_id_format"),
        CheckConstraint("mask_scale >= 1", name="mask_scale_at_least_one"),
        CheckConstraint("reference_frame_index >= 0", name="reference_frame_non_negative"),
    )


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[uuid_pk]
    kind: Mapped[str]  # "legacy_import" | "pipeline" | "backfill"
    status: Mapped[IngestStatus] = mapped_column(
        Enum(IngestStatus, name="ingest_status", native_enum=True),
        default=IngestStatus.QUEUED,
        server_default=text("'QUEUED'"),
    )

    release_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("releases.id", ondelete="SET NULL")
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    params: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default=text("'{}'"))
    error: Mapped[str | None]

    records_total: Mapped[int | None]
    records_done: Mapped[int] = mapped_column(default=0, server_default=text("0"))

    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]

    __table_args__ = (Index("ix_ingest_runs_status_created", "status", "created_at"),)
