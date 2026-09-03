"""Request dependencies: sessions, identity, permissions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.config import get_settings
from wascat.core.db import get_session
from wascat.core.errors import ForbiddenError, UnauthorizedError
from wascat.core.security.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SAFE_METHODS,
    tokens_match,
)
from wascat.core.security.tokens import AccessClaims, InvalidTokenError, decode_access_token
from wascat.storage.base import ObjectStore
from wascat.storage.local import LocalObjectStore
from wascat.storage.s3 import S3ObjectStore

ACCESS_COOKIE_NAME = "wascat_at"
REFRESH_COOKIE_NAME = "wascat_rt"

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return token if scheme.lower() == "bearer" and token else None


async def current_claims(request: Request) -> AccessClaims:
    """The signed claims of whoever is making this request.

    A cookie or a bearer token; the difference matters only for CSRF, which is
    why it is recorded on the request rather than collapsed here.
    """
    bearer = _bearer_token(request)
    token = bearer or request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        raise UnauthorizedError

    try:
        claims = decode_access_token(token)
    except InvalidTokenError as exc:
        raise UnauthorizedError("Your session has expired. Sign in again.") from exc

    request.state.authenticated_by = "bearer" if bearer else "cookie"
    request.state.claims = claims
    return claims


CurrentClaims = Annotated[AccessClaims, Depends(current_claims)]


async def verify_csrf(request: Request) -> None:
    """Double-submit check on cookie-authenticated mutations.

    Skipped for safe methods, and for bearer tokens: those are supplied
    deliberately by a client that already holds them, so there is no ambient
    credential for another site to exploit.
    """
    if request.method in SAFE_METHODS:
        return
    if getattr(request.state, "authenticated_by", None) == "bearer":
        return

    if not tokens_match(
        request.cookies.get(CSRF_COOKIE_NAME), request.headers.get(CSRF_HEADER_NAME)
    ):
        raise ForbiddenError(
            "This request could not be verified. Reload the page and try again.",
            code="csrf_failed",
        )


def require_permission(*permissions: str) -> Callable[..., Awaitable[AccessClaims]]:
    """Require every named permission.

    Checked against the signed claims, so it costs no query. The database
    enforces its own rules regardless - this decides who may ask, not what is
    allowed to be true.
    """

    async def dependency(claims: CurrentClaims) -> AccessClaims:
        missing = [name for name in permissions if name not in claims.permissions]
        if missing:
            raise ForbiddenError(
                f"This action needs the {', '.join(missing)} permission."
                if len(missing) == 1
                else f"This action needs the {', '.join(missing)} permissions."
            )
        return claims

    return dependency


# ---------------------------------------------------------------------------
# Object storage
# ---------------------------------------------------------------------------
#
# One client for the process, opened on first use. Creating an aiobotocore
# client loads service models and resolves endpoints, which is slow enough
# that doing it per request is the classic throughput bug in async S3 code.
_store: ObjectStore | None = None


async def get_store() -> ObjectStore:
    global _store  # noqa: PLW0603
    if _store is not None:
        return _store

    settings = get_settings()
    if settings.storage_backend == "local":
        _store = LocalObjectStore(
            settings.local_storage_root,
            public_base_url=settings.public_asset_base_url,
        )
    else:
        s3 = S3ObjectStore.from_settings()
        await s3.connect()
        _store = s3
    return _store


async def close_store() -> None:
    """Called from the app lifespan on shutdown."""
    global _store  # noqa: PLW0603
    if isinstance(_store, S3ObjectStore):
        await _store.close()
    _store = None


StoreDep = Annotated[ObjectStore, Depends(get_store)]
