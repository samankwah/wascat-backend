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
from wascat.core.errors import ForbiddenError, UnauthorizedError
from wascat.core.security.csrf import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SAFE_METHODS,
    tokens_match,
)

# `api` sits above `domains` in the layering, so this direction is allowed -
# it is the inverse (a domain importing `api`) that inverts the architecture.
from wascat.domains.iam import service as iam
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


async def assert_access_not_withdrawn(request: Request, db: SessionDep) -> None:
    """Refuse a token whose account has since been disabled or demoted.

    Permissions are signed into the access token and `require_permission` reads
    them from there, which is what keeps authorisation free of a query. The
    cost is a window: for the lifetime of an already-issued token, the claims
    say what was true when it was minted rather than what is true now. Revoking
    refresh sessions does not close that window - it only stops a *new* token
    being issued.

    For a role change that is untidy. For a disable it is a hole: withdrawing
    access from a compromised account would leave it writing for another
    quarter of an hour.

    So the admin surface pays for one indexed lookup per request and checks.
    That trade is only defensible here: this dependency guards the dashboard,
    used by a handful of curators, and never the public read API. `/me` already
    reads through to the database for exactly this reason.

    Additive changes are deliberately not disruptive - being *given* a
    permission mid-session does not invalidate anything. Only the removal of
    something the token still claims does.
    """
    claims = getattr(request.state, "claims", None)
    if claims is None:
        return

    user = await iam.get_user(db, claims.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("This account is no longer active. Sign in again.")

    withdrawn = set(claims.permissions) - user.permissions
    if withdrawn:
        raise UnauthorizedError(
            "Your access has changed since you signed in. Sign in again.",
        )


__all__ = [
    "ACCESS_COOKIE_NAME",
    "REFRESH_COOKIE_NAME",
    "CurrentClaims",
    "SessionDep",
    "StoreDep",
    "assert_access_not_withdrawn",
    "close_store",
    "current_claims",
    "get_store",
    "require_permission",
    "verify_csrf",
]
