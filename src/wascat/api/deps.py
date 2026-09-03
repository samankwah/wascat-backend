"""API-layer request dependencies.

Only the genuinely API-shaped piece lives here: the CSRF check, which is about
how a browser sends a request rather than about the domain it reaches. The
session, store and permission dependencies live in ``core.deps``, because the
domain routers need them and a domain importing from ``api`` would invert the
architecture.

Re-exported here so route modules can keep importing from one place.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from wascat.core.deps import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    CurrentClaims,
    SessionDep,
    current_claims,
    require_permission,
)
from wascat.core.errors import ForbiddenError
from wascat.core.security.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SAFE_METHODS,
    tokens_match,
)
from wascat.storage.base import ObjectStore
from wascat.storage.factory import close_store, get_store

StoreDep = Annotated[ObjectStore, Depends(get_store)]


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


__all__ = [
    "ACCESS_COOKIE_NAME",
    "REFRESH_COOKIE_NAME",
    "CurrentClaims",
    "SessionDep",
    "StoreDep",
    "close_store",
    "current_claims",
    "get_store",
    "require_permission",
    "verify_csrf",
]
