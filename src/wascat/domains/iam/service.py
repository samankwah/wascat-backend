"""Authentication and session lifecycle."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from wascat.core.config import get_settings
from wascat.core.errors import ForbiddenError, UnauthorizedError
from wascat.core.security.passwords import hash_password, verify_password
from wascat.core.security.tokens import (
    create_access_token,
    hash_refresh_token,
    issue_refresh_token,
)
from wascat.domains.iam.models import LoginAttempt, RefreshSession, Role, User

#: Failed sign-ins allowed per email before it is locked out for a while.
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW = timedelta(minutes=15)

#: Hashing a password nobody has is pointless work, but skipping it tells an
#: attacker which addresses exist by how quickly the request comes back. This
#: is verified against instead, so an unknown email costs the same as a known
#: one. It corresponds to no password: the hash is of random bytes.
_DUMMY_HASH: str | None = None


@dataclass(frozen=True, slots=True)
class Session:
    user: User
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


class RateLimitedError(UnauthorizedError):
    code = "too_many_attempts"
    message = "Too many sign-in attempts. Try again later."
    status_code = 429


class InvalidCredentialsError(UnauthorizedError):
    code = "invalid_credentials"
    # Deliberately does not distinguish "no such account" from "wrong
    # password": which one it is, is not the caller's business.
    message = "Email or password is incorrect."


class AccountDisabledError(ForbiddenError):
    code = "account_disabled"
    message = "This account has been disabled."


async def _dummy_hash() -> str:
    global _DUMMY_HASH  # noqa: PLW0603
    if _DUMMY_HASH is None:
        _DUMMY_HASH = await hash_password(uuid.uuid4().hex)
    return _DUMMY_HASH


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    stmt = (
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.email == email)
    )
    return (await session.execute(stmt)).scalars().first()


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    stmt = (
        select(User)
        .options(selectinload(User.roles).selectinload(Role.permissions))
        .where(User.id == user_id)
    )
    return (await session.execute(stmt)).scalars().first()


async def _recent_failures(session: AsyncSession, email: str) -> int:
    since = datetime.now(UTC) - LOGIN_WINDOW
    stmt = select(func.count(LoginAttempt.id)).where(
        LoginAttempt.email == email,
        LoginAttempt.successful.is_(False),
        LoginAttempt.created_at >= since,
    )
    return (await session.execute(stmt)).scalar_one()


async def authenticate(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> Session:
    """Verify credentials and open a session."""
    if await _recent_failures(session, email) >= MAX_LOGIN_ATTEMPTS:
        raise RateLimitedError

    user = await get_user_by_email(session, email)

    # Always spend the time, whether or not the account exists.
    stored_hash = user.password_hash if user else await _dummy_hash()
    valid, updated_hash = await verify_password(password, stored_hash)

    if user is None or not valid:
        session.add(LoginAttempt(email=email, ip=ip, successful=False))
        await session.flush()
        raise InvalidCredentialsError

    if not user.is_active:
        session.add(LoginAttempt(email=email, ip=ip, successful=False))
        await session.flush()
        raise AccountDisabledError

    # The hashing parameters have been strengthened since this was set, so
    # take the opportunity while the plaintext is in hand.
    if updated_hash:
        user.password_hash = updated_hash

    user.last_login_at = datetime.now(UTC)
    session.add(LoginAttempt(email=email, ip=ip, successful=True))

    return await _open_session(
        session, user=user, family_id=uuid.uuid4(), user_agent=user_agent, ip=ip
    )


async def _open_session(
    session: AsyncSession,
    *,
    user: User,
    family_id: uuid.UUID,
    user_agent: str | None,
    ip: str | None,
    replaces: RefreshSession | None = None,
) -> Session:
    settings = get_settings()
    access_token, access_expires_at = create_access_token(
        user_id=user.id,
        email=user.email,
        roles={role.slug for role in user.roles},
        permissions=user.permissions,
    )
    refresh_token, token_hash = issue_refresh_token()
    refresh_expires_at = datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds)

    record = RefreshSession(
        user_id=user.id,
        family_id=family_id,
        token_hash=token_hash,
        expires_at=refresh_expires_at,
        user_agent=user_agent,
        ip=ip,
    )
    session.add(record)
    await session.flush()

    if replaces is not None:
        replaces.replaced_by_id = record.id

    return Session(
        user=user,
        access_token=access_token,
        access_expires_at=access_expires_at,
        refresh_token=refresh_token,
        refresh_expires_at=refresh_expires_at,
    )


async def refresh(
    session: AsyncSession,
    *,
    token: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> Session:
    """Rotate a refresh token, detecting replay.

    Each refresh revokes the token presented and issues a successor in the same
    family. Presenting a token that is already revoked therefore means it was
    used twice - either the legitimate holder replayed it, or it was stolen and
    someone else got there first. There is no way to tell which, so the whole
    family is revoked and everyone signs in again. A stolen token buys at most
    one window.
    """
    token_hash = hash_refresh_token(token)
    stmt = select(RefreshSession).where(RefreshSession.token_hash == token_hash)
    record = (await session.execute(stmt)).scalars().first()

    if record is None:
        raise UnauthorizedError("This session is no longer valid.")

    now = datetime.now(UTC)

    if record.revoked_at is not None:
        await revoke_family(session, record.family_id)
        raise UnauthorizedError(
            "This session was reused and has been ended everywhere. Sign in again."
        )

    if record.expires_at <= now:
        raise UnauthorizedError("This session has expired.")

    user = await get_user(session, record.user_id)
    if user is None or not user.is_active:
        await revoke_family(session, record.family_id)
        raise AccountDisabledError

    record.revoked_at = now
    return await _open_session(
        session,
        user=user,
        family_id=record.family_id,
        user_agent=user_agent,
        ip=ip,
        replaces=record,
    )


async def revoke(session: AsyncSession, *, token: str) -> None:
    """End one session. Signing out is not an error even if it is already out."""
    await session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.token_hash == hash_refresh_token(token),
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_family(session: AsyncSession, family_id: uuid.UUID) -> None:
    await session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.family_id == family_id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def revoke_all_for_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Used when an account is disabled or its password changes."""
    await session.execute(
        update(RefreshSession)
        .where(
            RefreshSession.user_id == user_id,
            RefreshSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str | None = None,
    role_slugs: list[str] | None = None,
) -> User:
    user = User(
        email=email,
        password_hash=await hash_password(password),
        full_name=full_name,
    )
    if role_slugs:
        roles = (
            (await session.execute(select(Role).where(Role.slug.in_(role_slugs)))).scalars().all()
        )
        user.roles = list(roles)
    session.add(user)
    await session.flush()
    return user


async def set_password(session: AsyncSession, *, user: User, password: str) -> None:
    user.password_hash = await hash_password(password)
    # Changing a password ends every other session; otherwise a compromised
    # one survives the very act meant to end it.
    await revoke_all_for_user(session, user.id)
