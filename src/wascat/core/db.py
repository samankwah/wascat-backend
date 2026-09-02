"""Database engine, session factory and declarative base."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import ARRAY, DateTime, MetaData, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, mapped_column

from wascat.core.config import get_settings

# Predictable constraint names make migrations reviewable and let tests assert
# on which constraint rejected a write.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base.

    ``AsyncAttrs`` matters: it provides ``await obj.awaitable_attrs.artifacts``
    so a relationship someone forgot to eager-load raises a clear error rather
    than a bare ``MissingGreenlet`` from deep inside SQLAlchemy.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
        datetime: DateTime(timezone=True),
        # Cloud fraction is stored exactly, never as a float. See risk R5.
        Decimal: Numeric(9, 6),
        str: Text,
        uuid.UUID: UUID(as_uuid=True),
    }


# -- Column annotation aliases ---------------------------------------------

uuid_pk = Annotated[
    uuid.UUID, mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
]
ts_created = Annotated[datetime, mapped_column(server_default=func.now())]
ts_updated = Annotated[datetime, mapped_column(server_default=func.now(), onupdate=func.now())]

# Aliases are annotation-position only: `Mapped[uuid_pk]`, never
# `mapped_column(uuid_pk)`. Columns needing extra keywords (nullable,
# server_default) spell their type out instead, which reads more plainly than
# splitting the definition across an alias and a call.


# -- Engine / session ------------------------------------------------------

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine(url: str | None = None) -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        url or settings.database_url_str,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        # NOTE: if a transaction-pooling PgBouncer is ever put in front of
        # Postgres, asyncpg's prepared-statement cache must be disabled
        # (statement_cache_size=0) and the pool switched to NullPool, or
        # queries fail intermittently with "prepared statement already
        # exists". See risk R18.
    )


def get_engine() -> AsyncEngine:
    # One engine per process, created lazily so importing this module never
    # opens a connection pool (tests and the CLI rely on that).
    global _engine  # noqa: PLW0603
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker  # noqa: PLW0603
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session bound to the request."""
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _sessionmaker  # noqa: PLW0603
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
