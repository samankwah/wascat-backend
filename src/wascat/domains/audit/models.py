"""Audit log.

This is a scientific archive, so the provenance of an *edit* matters as much
as the provenance of a measurement. Every admin mutation records what changed,
who changed it and when, with the actor's email snapshotted so the trail
survives the user being deleted.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from wascat.core.db import Base, ts_created


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    # Snapshot, so the trail stays readable after the account is removed.
    actor_email: Mapped[str | None]

    # Dotted verb, e.g. "image_record.update", "release.publish", "vocab.merge".
    action: Mapped[str]
    entity_type: Mapped[str]
    entity_id: Mapped[str]

    before: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    summary: Mapped[str | None]

    request_id: Mapped[str | None]
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    created_at: Mapped[ts_created]

    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_events_action_created", "action", "created_at"),
        Index("ix_audit_events_created", "created_at"),
    )
