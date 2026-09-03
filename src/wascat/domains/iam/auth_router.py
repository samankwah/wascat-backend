"""Sign in, refresh, sign out, and who am I.

Tokens travel in cookies rather than in the response body. The dashboard runs
same-origin with this API through the frontend's rewrite, so the browser
attaches them automatically and JavaScript never touches the access or refresh
token - which means an XSS bug on the page cannot read a session out of it.

The CSRF token is the deliberate exception: it must be readable, because the
page has to echo it back in a header.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Cookie, Request, Response
from pydantic import BaseModel, Field

from wascat.api.deps import (
    ACCESS_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    CurrentClaims,
    SessionDep,
)
from wascat.core.config import get_settings
from wascat.core.envelope import CanonicalJSONResponse, envelope
from wascat.core.errors import UnauthorizedError
from wascat.core.security.csrf import CSRF_COOKIE_NAME, issue_csrf_token
from wascat.domains.iam import service
from wascat.domains.iam.service import Session

router = APIRouter(prefix="/auth", tags=["admin:auth"])

#: The refresh token is only ever sent to the endpoints that rotate it, so it
#: is not attached to every API call the dashboard makes.
REFRESH_COOKIE_PATH = "/api/v1/admin/auth"


class Credentials(BaseModel):
    # Deliberately not EmailStr. The address is a sign-in identifier, not a
    # destination - nothing is ever sent to it - and EmailStr enforces
    # deliverability, which rejects the reserved domains real deployments use
    # internally (admin@wascat.local, anything on .internal). A shape check is
    # what is actually wanted here.
    email: Annotated[
        str,
        Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$"),
    ]
    password: Annotated[str, Field(min_length=1, max_length=512)]


def _client(request: Request) -> tuple[str | None, str | None]:
    user_agent = request.headers.get("user-agent")
    ip = request.client.host if request.client else None
    return user_agent, ip


def _set_session_cookies(response: Response, session: Session, csrf_token: str) -> None:
    settings = get_settings()
    common: dict[str, Any] = {
        "secure": settings.cookie_secure,
        "domain": settings.cookie_domain,
    }

    response.set_cookie(
        ACCESS_COOKIE_NAME,
        session.access_token,
        httponly=True,
        samesite="lax",
        path="/api/v1",
        expires=session.access_expires_at,
        **common,
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        session.refresh_token,
        httponly=True,
        # Strict: this cookie exists only to mint new sessions, and no
        # cross-site navigation has any business carrying it.
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
        expires=session.refresh_expires_at,
        **common,
    )

    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        # Readable on purpose: the page has to echo it back in a header, which
        # is precisely what a cross-site page cannot do.
        httponly=False,
        samesite="lax",
        path="/api/v1",
        expires=session.refresh_expires_at,
        **common,
    )


def _clear_session_cookies(response: Response) -> None:
    settings = get_settings()
    for name, path in (
        (ACCESS_COOKIE_NAME, "/api/v1"),
        (REFRESH_COOKIE_NAME, REFRESH_COOKIE_PATH),
        (CSRF_COOKIE_NAME, "/api/v1"),
    ):
        response.delete_cookie(name, path=path, domain=settings.cookie_domain)


def _identity(session: Session, csrf_token: str) -> dict[str, Any]:
    user = session.user
    return {
        "id": str(user.id),
        "email": user.email,
        "fullName": user.full_name,
        "roles": sorted(role.slug for role in user.roles),
        "permissions": sorted(user.permissions),
        "csrfToken": csrf_token,
        "accessExpiresAt": session.access_expires_at.isoformat(),
    }


@router.post("/login", summary="Sign in")
async def login(
    credentials: Credentials, request: Request, db: SessionDep
) -> CanonicalJSONResponse:
    user_agent, ip = _client(request)
    session = await service.authenticate(
        db,
        email=credentials.email.strip(),
        password=credentials.password,
        user_agent=user_agent,
        ip=ip,
    )
    await db.commit()

    csrf_token = issue_csrf_token()
    response = envelope(request, _identity(session, csrf_token))
    _set_session_cookies(response, session, csrf_token)
    return response


@router.post("/refresh", summary="Rotate the session")
async def refresh(
    request: Request,
    db: SessionDep,
    wascat_rt: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
) -> CanonicalJSONResponse:
    if not wascat_rt:
        raise UnauthorizedError("No session to refresh.")

    user_agent, ip = _client(request)
    try:
        session = await service.refresh(db, token=wascat_rt, user_agent=user_agent, ip=ip)
    except UnauthorizedError:
        await db.commit()  # keep the revocations the reuse check performed
        raise

    await db.commit()

    csrf_token = issue_csrf_token()
    response = envelope(request, _identity(session, csrf_token))
    _set_session_cookies(response, session, csrf_token)
    return response


@router.post("/logout", summary="Sign out")
async def logout(
    request: Request,
    db: SessionDep,
    wascat_rt: Annotated[str | None, Cookie(alias=REFRESH_COOKIE_NAME)] = None,
) -> CanonicalJSONResponse:
    # Signing out is never an error, even when there is nothing to sign out of.
    if wascat_rt:
        await service.revoke(db, token=wascat_rt)
        await db.commit()

    response = envelope(request, {"signedOut": True})
    _clear_session_cookies(response)
    return response


@router.get("/me", summary="The signed-in user")
async def me(request: Request, claims: CurrentClaims, db: SessionDep) -> CanonicalJSONResponse:
    # Read through to the database rather than trusting the token alone: a
    # role revoked a minute ago should take effect now, not when the access
    # token happens to expire.
    user = await service.get_user(db, claims.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("This account is no longer active.")

    return envelope(
        request,
        {
            "id": str(user.id),
            "email": user.email,
            "fullName": user.full_name,
            "roles": sorted(role.slug for role in user.roles),
            "permissions": sorted(user.permissions),
            "lastLoginAt": user.last_login_at.isoformat() if user.last_login_at else None,
        },
    )


__all__ = ["router"]
