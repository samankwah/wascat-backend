"""Dashboard endpoints for accounts and role assignments.

The first account is made with ``wascat users create``, because the dashboard
needs one to sign in. Everything after that happens here.

The rules live in ``admin_service`` rather than in these handlers, so they hold
for the operation itself rather than for one route into it. What is left here
is the HTTP shape and the audit trail.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from wascat.core.deps import SessionDep, require_permission
from wascat.core.envelope import CanonicalJSONResponse, envelope
from wascat.core.security.tokens import AccessClaims
from wascat.domains.audit import service as audit
from wascat.domains.iam import admin_service
from wascat.domains.iam.admin_schemas import UserCreate, UserUpdate
from wascat.domains.iam.models import ROLE_DESCRIPTIONS, USER_MANAGE

router = APIRouter(prefix="/users", tags=["admin:users"])

CanManage = Annotated[AccessClaims, Depends(require_permission(USER_MANAGE))]


def _actor(request: Request, claims: AccessClaims) -> dict[str, Any]:
    return {
        "actor_id": claims.user_id,
        "actor_email": claims.email,
        "request_id": getattr(request.state, "request_id", None),
        "ip": request.client.host if request.client else None,
    }


@router.get("", summary="Every account")
async def list_users(request: Request, db: SessionDep, claims: CanManage) -> CanonicalJSONResponse:
    users = await admin_service.list_users(db)
    roles = await admin_service.list_roles(db)
    return envelope(
        request,
        [admin_service.snapshot(user) for user in users],
        meta={
            "count": len(users),
            # Sent alongside so the role picker can describe what it offers
            # rather than showing three bare slugs.
            "roles": [
                {
                    "slug": role.slug,
                    "description": ROLE_DESCRIPTIONS.get(role.slug, ""),
                    "permissions": sorted(permission.slug for permission in role.permissions),
                }
                for role in roles
            ],
        },
    )


@router.post("", summary="Invite someone")
async def create_user(
    payload: UserCreate, request: Request, db: SessionDep, claims: CanManage
) -> CanonicalJSONResponse:
    user, password = await admin_service.create_user(db, payload)
    after = admin_service.snapshot(user)
    await audit.record(
        db,
        action="user.create",
        entity_type="user",
        entity_id=str(user.id),
        after=after,
        summary=f"invited {user.email} as {payload.role}",
        **_actor(request, claims),
    )
    await db.commit()
    # The only time this value is readable. Nothing stores it in plaintext, so
    # if it is lost the reset endpoint is the way back.
    return envelope(request, {**after, "password": password})


@router.patch("/{user_id}", summary="Change a name, a role, or access")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    db: SessionDep,
    claims: CanManage,
) -> CanonicalJSONResponse:
    user = await admin_service.get_user_or_404(db, user_id)
    before = admin_service.snapshot(user)
    user = await admin_service.update_user(
        db, user=user, payload=payload, acting_user_id=claims.user_id
    )
    after = admin_service.snapshot(user)

    if changed := audit.changed_fields(before, after):
        await audit.record(
            db,
            action="user.update",
            entity_type="user",
            entity_id=str(user.id),
            before=before,
            after=after,
            summary=f"updated {user.email} ({', '.join(changed)})",
            **_actor(request, claims),
        )
    await db.commit()
    return envelope(request, after)


@router.post("/{user_id}/password", summary="Reset a password")
async def reset_password(
    user_id: uuid.UUID, request: Request, db: SessionDep, claims: CanManage
) -> CanonicalJSONResponse:
    user = await admin_service.get_user_or_404(db, user_id)
    password = await admin_service.reset_password(db, user=user)
    await audit.record(
        db,
        action="user.password_reset",
        entity_type="user",
        entity_id=str(user.id),
        summary=f"reset the password for {user.email}",
        **_actor(request, claims),
    )
    await db.commit()
    await db.refresh(user)
    return envelope(request, {**admin_service.snapshot(user), "password": password})
