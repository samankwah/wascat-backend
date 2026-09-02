"""Users, roles, permissions and refresh sessions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Table,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wascat.core.db import Base, ts_created, ts_updated, uuid_pk

# -- Permission slugs -------------------------------------------------------

CATALOG_READ = "catalog:read"
CATALOG_WRITE = "catalog:write"
RELEASE_PUBLISH = "release:publish"
VOCAB_READ = "vocab:read"
VOCAB_WRITE = "vocab:write"
INGEST_RUN = "ingest:run"
USER_MANAGE = "user:manage"
AUDIT_READ = "audit:read"

ALL_PERMISSIONS: tuple[tuple[str, str], ...] = (
    (CATALOG_READ, "View image records, collections and releases in the dashboard"),
    (CATALOG_WRITE, "Create, edit and retire image records, collections and releases"),
    (RELEASE_PUBLISH, "Publish and retire releases"),
    (VOCAB_READ, "View controlled vocabularies"),
    (VOCAB_WRITE, "Add, rename, merge and reorder vocabulary terms"),
    (INGEST_RUN, "Start ingest runs and backfills"),
    (USER_MANAGE, "Manage users and role assignments"),
    (AUDIT_READ, "Read the audit log"),
)

#: Seeded roles. `curator` is the day-to-day editorial role: it can change the
#: catalogue and the vocabulary but cannot publish a release or manage users.
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "admin": tuple(slug for slug, _ in ALL_PERMISSIONS),
    "curator": (CATALOG_READ, CATALOG_WRITE, VOCAB_READ, VOCAB_WRITE, INGEST_RUN),
    "viewer": (CATALOG_READ, VOCAB_READ, AUDIT_READ),
}

ROLE_DESCRIPTIONS = {
    "admin": "Full access, including publishing, user management and the audit log",
    "curator": "Edits the catalogue and vocabularies; cannot publish or manage users",
    "viewer": "Read-only access to the dashboard",
}


user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


class Permission(Base):
    __tablename__ = "permissions"

    id: Mapped[uuid_pk]
    slug: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None]


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[uuid_pk]
    slug: Mapped[str] = mapped_column(unique=True)
    description: Mapped[str | None]
    system: Mapped[bool] = mapped_column(default=False, server_default=text("false"))

    permissions: Mapped[list[Permission]] = relationship(
        secondary=role_permissions, lazy="selectin"
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid_pk]
    # CITEXT so "Ada@example.org" and "ada@example.org" are the same account.
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str]
    full_name: Mapped[str | None]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    last_login_at: Mapped[datetime | None]
    created_at: Mapped[ts_created]
    updated_at: Mapped[ts_updated]

    roles: Mapped[list[Role]] = relationship(secondary=user_roles, lazy="selectin")

    __table_args__ = (CheckConstraint("position('@' in email) > 1", name="email_shape"),)

    @property
    def permissions(self) -> set[str]:
        return {permission.slug for role in self.roles for permission in role.permissions}


class RefreshSession(Base):
    """One issued refresh token.

    Only the SHA-256 of the token is stored, so a database leak does not hand
    over live sessions. Rotation forms a family: refreshing revokes the old row
    and inserts a successor with the same ``family_id``. Presenting an already
    revoked token means it was replayed, so the whole family is revoked - a
    stolen token buys at most one window.
    """

    __tablename__ = "refresh_sessions"

    id: Mapped[uuid_pk]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    family_id: Mapped[uuid.UUID]
    token_hash: Mapped[str] = mapped_column(unique=True)

    issued_at: Mapped[ts_created]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_sessions.id", ondelete="SET NULL")
    )

    user_agent: Mapped[str | None]
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    __table_args__ = (
        Index("ix_refresh_sessions_user_id", "user_id"),
        Index("ix_refresh_sessions_family_id", "family_id"),
        Index(
            "ix_refresh_sessions_active",
            "user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class LoginAttempt(Base):
    """Throttling record for the login endpoint.

    Kept in Postgres rather than memory so the limit survives a restart and
    holds across replicas.
    """

    __tablename__ = "login_attempts"

    id: Mapped[uuid_pk]
    email: Mapped[str] = mapped_column(CITEXT)
    ip: Mapped[str | None] = mapped_column(INET, nullable=True)
    successful: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    created_at: Mapped[ts_created]

    __table_args__ = (
        Index("ix_login_attempts_email_created", "email", "created_at"),
        Index("ix_login_attempts_ip_created", "ip", "created_at"),
    )
