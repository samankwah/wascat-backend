"""Database fixtures.

Two decisions worth stating, because they are the difference between a suite
that runs and one that fights you:

* The schema is built by ``alembic upgrade head``, never by
  ``metadata.create_all``. Half the domain invariants live in triggers that
  only the migrations create, so ``create_all`` would produce a database that
  passes tests the real one would fail.

* Each test runs inside a transaction that is rolled back, with the session
  joined to it via a savepoint. That is fast, needs no truncation, and lets
  service code call ``commit()`` normally. The catch is that DEFERRED
  constraints fire at savepoint release rather than at the outer COMMIT, so a
  test asserting one must ``commit()`` or it passes vacuously.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TEST_URL = "postgresql+asyncpg://wascat:wascat@localhost:5432/wascat_test"
ADMIN_URL = "postgresql+asyncpg://wascat:wascat@localhost:5432/postgres"


def resolve_database_url() -> str:
    return os.environ.get("WASCAT_TEST_DATABASE_URL", DEFAULT_TEST_URL)


async def _ensure_database(url: str) -> None:
    """(Re)create the test database from scratch.

    Rebuilding rather than upgrading is deliberate while the schema is still
    moving. Alembic records only the head revision, so regenerating an
    *intermediate* migration leaves the database stamped at a head that looks
    current while its tables are stale. ``upgrade head`` then does nothing and
    the suite runs green (or red) against a schema nobody wrote. That happened
    once during this port; a fresh database each session makes it impossible.

    Set WASCAT_TEST_REUSE_DB=1 to skip the rebuild once the schema settles.
    """
    name = url.rsplit("/", 1)[-1]

    async def _run(statement: str) -> None:
        engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                await conn.exec_driver_sql(statement)
        finally:
            await engine.dispose()

    async def _exists() -> bool:
        engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                # asyncpg speaks $1 placeholders, not psycopg's %(name)s, so go
                # through SQLAlchemy's bound parameters, not the raw driver.
                result = await conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
                )
                return result.scalar_one_or_none() is not None
        finally:
            await engine.dispose()

    reuse = os.environ.get("WASCAT_TEST_REUSE_DB") == "1"
    exists = await _exists()
    if exists and not reuse:
        await _run(f'DROP DATABASE "{name}" WITH (FORCE)')
        exists = False
    if not exists:
        await _run(f'CREATE DATABASE "{name}"')


def _migrate(url: str) -> None:
    env = {**os.environ, "WASCAT_DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")


@pytest.fixture(scope="session")
async def engine():  # type: ignore[no-untyped-def]
    url = resolve_database_url()
    try:
        await _ensure_database(url)
    except OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            f"PostgreSQL is not reachable ({exc.__class__.__name__}). Run `.\\tasks.ps1 up`."
        )
    _migrate(url)

    engine = create_async_engine(url, poolclass=None)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:  # type: ignore[no-untyped-def]
    """A session whose writes are rolled back when the test finishes."""
    async with engine.connect() as connection:
        transaction = await connection.begin()
        maker = async_sessionmaker(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with maker() as db_session:
            try:
                yield db_session
            finally:
                await db_session.close()
        await transaction.rollback()


async def run_deferred_checks(db_session: AsyncSession) -> None:
    """Make DEFERRED constraints fire now.

    Deferred constraints and constraint triggers run at COMMIT. Every test here
    lives inside a transaction that is rolled back, and the savepoint release
    that ``session.commit()`` performs is not a commit, so they would never run
    - and a test asserting one would pass without exercising anything.

    Flushing and switching the constraints to IMMEDIATE evaluates them against
    everything written so far, which is what the assertion actually means.
    """
    await db_session.flush()
    await db_session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
