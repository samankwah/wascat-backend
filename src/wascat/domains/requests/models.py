"""Requests for the archive's data.

Downloads are off for now: a researcher asks, an administrator decides, and an
approved requester gets a private, expiring link to the files. This table is
the whole record of that exchange.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import ARRAY, CheckConstraint, Enum, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import CITEXT, INET
from sqlalchemy.orm import Mapped, mapped_column

from wascat.core.db import Base, ts_created, ts_updated, uuid_pk


class DataRequestStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"


class DataRequest(Base):
    __tablename__ = "data_requests"

    id: Mapped[uuid_pk]
    # What the requester and the administrators quote to each other, e.g.
    # "DR-7K3QX". The id is for URLs; this is for people.
    reference: Mapped[str] = mapped_column(unique=True)
    status: Mapped[DataRequestStatus] = mapped_column(
        Enum(DataRequestStatus, name="data_request_status", native_enum=True),
        default=DataRequestStatus.PENDING,
        server_default=text("'PENDING'"),
    )

    name: Mapped[str]
    # CITEXT so the per-address rate limit cannot be dodged with capitals.
    email: Mapped[str] = mapped_column(CITEXT)
    organisation: Mapped[str]
    purpose: Mapped[str]
    collections: Mapped[list[str]] = mapped_column(ARRAY(Text))
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    reviewed_at: Mapped[datetime | None]
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Shown to the requester: the reason for a decline, or a note with the data.
    decision_note: Mapped[str | None]

    # Only the SHA-256 of the access token is kept, as for refresh sessions,
    # so the table alone cannot be turned into download links.
    access_token_hash: Mapped[str | None] = mapped_column(unique=True)
    access_expires_at: Mapped[datetime | None]

    admins_notified_at: Mapped[datetime | None]
    requester_notified_at: Mapped[datetime | None]
    # The last mail failure, if any, so the dashboard can offer to resend.
    last_mail_error: Mapped[str | None]

    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]

    __table_args__ = (
        CheckConstraint("cardinality(collections) > 0", name="asks_for_something"),
        CheckConstraint(
            "status = 'PENDING' OR reviewed_at IS NOT NULL", name="decided_requests_are_reviewed"
        ),
        CheckConstraint(
            "status <> 'APPROVED' OR (access_token_hash IS NOT NULL AND access_expires_at IS NOT NULL)",
            name="approved_requests_have_access",
        ),
        CheckConstraint(
            "status <> 'DECLINED' OR decision_note IS NOT NULL", name="declines_give_a_reason"
        ),
        Index("ix_data_requests_status_created_at", "status", "created_at"),
        Index("ix_data_requests_email_created_at", "email", "created_at"),
        Index("ix_data_requests_ip_created_at", "ip", "created_at"),
    )
