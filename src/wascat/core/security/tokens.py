"""Session tokens.

Two kinds, deliberately different:

* The **access token** is a short-lived JWT. It carries the user's id, roles
  and permissions, so authorising a request costs no database round trip.
  Being self-contained it also cannot be revoked, which is why it is short.

* The **refresh token** is opaque random bytes, and only its SHA-256 is
  stored. A database leak therefore hands over no usable sessions, and
  because the server holds state for it, it can be revoked immediately.

PyJWT rather than python-jose: FastAPI's documentation migrated for it, and
python-jose has had long maintenance gaps.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from wascat.core.config import get_settings

ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - a claim value, not a secret

REFRESH_TOKEN_BYTES = 32


class InvalidTokenError(Exception):
    """The token is missing, malformed, expired, or not the expected type."""


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: uuid.UUID
    email: str
    roles: frozenset[str]
    permissions: frozenset[str]
    token_id: str
    expires_at: datetime


def create_access_token(
    *,
    user_id: uuid.UUID,
    email: str,
    roles: set[str],
    permissions: set[str],
) -> tuple[str, datetime]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "email": email,
        "roles": sorted(roles),
        "perms": sorted(permissions),
        "typ": ACCESS_TOKEN_TYPE,
        "jti": secrets.token_urlsafe(12),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM), expires_at


def decode_access_token(token: str) -> AccessClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        # A refresh token must never be accepted where an access token is
        # expected: it lives far longer and is meant for one endpoint.
        raise InvalidTokenError("Not an access token")

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError("Malformed subject") from exc

    return AccessClaims(
        user_id=user_id,
        email=str(payload.get("email", "")),
        roles=frozenset(payload.get("roles", [])),
        permissions=frozenset(payload.get("perms", [])),
        token_id=str(payload.get("jti", "")),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )


def issue_refresh_token() -> tuple[str, str]:
    """Return ``(token, hash)``. Only the hash is ever stored."""
    token = secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
