"""Router assembly.

Two audiences under one prefix. `/api/v1/...` is the public read API, open to
anyone. `/api/v1/admin/...` requires a session, and every mutation additionally
carries a CSRF token.

Authentication is declared on the admin router itself rather than per route, so
a new endpoint is protected by where it is mounted instead of by whoever
remembers the decorator. The auth endpoints are the deliberate exception -
sign-in cannot require being signed in - and are mounted separately.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from wascat.api.deps import current_claims, verify_csrf
from wascat.domains.catalog.router import router as catalog_router
from wascat.domains.iam.auth_router import router as auth_router

public_router = APIRouter()
public_router.include_router(catalog_router)

# Open: signing in cannot require a session, and refresh proves itself with the
# refresh cookie rather than an access token.
admin_public_router = APIRouter(prefix="/api/v1/admin")
admin_public_router.include_router(auth_router)

# Everything else behind a session.
admin_router = APIRouter(
    prefix="/api/v1/admin",
    dependencies=[Depends(current_claims), Depends(verify_csrf)],
)

__all__ = ["admin_public_router", "admin_router", "public_router"]
