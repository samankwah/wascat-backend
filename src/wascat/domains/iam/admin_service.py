"""Managing accounts and role assignments.

The rules live here rather than in the router so that they are true of the
operation itself, not of one way of reaching it - the same reason
`catalog/admin_service` holds the release rules. They can then be tested
directly, without a signed session and a CSRF token standing in the way of
asserting a guard.

Four of them, each protecting against a failure that is silent rather than
loud:

* **Passwords are generated, never supplied.** These functions mint one and
  return it exactly once. No caller can set a chosen password on someone
  else's account, so nothing weak arrives from a form.
* **A role change or a disable ends that account's sessions.** Permissions
  travel in a signed access token and are not re-read per request, so without
  this a demoted curator keeps their old access until the token expires.
* **Nobody can lock themselves, or everybody, out.** You cannot drop your own
  ability to manage people, and the last account that can manage people cannot
  be disabled or demoted.
* **Accounts are disabled, never deleted.** Audit events name their actor, and
  a deleted row leaves the archive's history pointing at nobody.
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.errors import ConflictError, NotFoundError
from wascat.core.jsformat import iso_z
from wascat.domains.iam import service
from wascat.domains.iam.admin_schemas import UserCreate, UserUpdate
from wascat.domains.iam.models import (
    USER_MANAGE,
    Permission,
    Role,
    User,
    role_permissions,
)

#: Long enough not to need a complexity rule to be strong, short enough to read
#: off a screen once. Matches what `wascat users create` generates.
GENERATED_PASSWORD_BYTES = 18


def generate_password() -> str:
    return secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)


def snapshot(user: User) -> dict[str, Any]:
    """The public shape of an account.

    ``password_hash`` is absent by construction rather than by filtering, so
    adding a field later cannot leak it by forgetting to exclude it. This is
    what both the API returns and the audit log records.
    """
    return {
        "id": str(user.id),
        "email": user.email,
        "fullName": user.full_name,
        "roles": sorted(role.slug for role in user.roles),
        "permissions": sorted(user.permissions),
        "isActive": user.is_active,
        "lastLoginAt": iso_z(user.last_login_at) if user.last_login_at else None,
        "createdAt": iso_z(user.created_at),
    }


async def get_role_or_404(session: AsyncSession, slug: str) -> Role:
    role = (await session.execute(select(Role).where(Role.slug == slug))).scalars().first()
    if role is None:
        known = (await session.execute(select(Role.slug).order_by(Role.slug))).scalars().all()
        raise NotFoundError(f"No such role '{slug}'. Available: {', '.join(known)}.")
    return role


async def get_user_or_404(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await service.get_user(session, user_id)
    if user is None:
        raise NotFoundError("No such account.")
    return user


async def list_users(session: AsyncSession) -> list[User]:
    return list((await session.execute(select(User).order_by(User.email))).scalars().all())


async def list_roles(session: AsyncSession) -> list[Role]:
    return list((await session.execute(select(Role).order_by(Role.slug))).scalars().all())


def can_manage_people(user: User) -> bool:
    return USER_MANAGE in user.permissions


def _role_grants_management(role: Role) -> bool:
    return USER_MANAGE in {permission.slug for permission in role.permissions}


async def _other_managers(session: AsyncSession, *, excluding: uuid.UUID) -> int:
    """Active accounts that can still manage people, ignoring one of them.

    Counts the permission rather than the role slug, so a role added later that
    also grants ``user:manage`` is included without this being revisited.
    """
    stmt = (
        select(func.count(func.distinct(User.id)))
        .select_from(User)
        .join(User.roles)
        .join(role_permissions, Role.id == role_permissions.c.role_id)
        .join(Permission, Permission.id == role_permissions.c.permission_id)
        .where(
            Permission.slug == USER_MANAGE,
            User.is_active.is_(True),
            User.id != excluding,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def create_user(session: AsyncSession, payload: UserCreate) -> tuple[User, str]:
    """Invite someone. Returns the account and its one-time password."""
    if await service.get_user_by_email(session, payload.email) is not None:
        raise ConflictError(f"{payload.email} already has an account.")

    role = await get_role_or_404(session, payload.role)
    password = generate_password()
    user = await service.create_user(
        session,
        email=payload.email,
        password=password,
        full_name=payload.full_name,
        role_slugs=[role.slug],
    )
    return user, password


async def update_user(
    session: AsyncSession,
    *,
    user: User,
    payload: UserUpdate,
    acting_user_id: uuid.UUID,
) -> User:
    """Apply a change, refusing the ones that would strand somebody."""
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return user

    new_role = await get_role_or_404(session, changes["role"]) if "role" in changes else None
    disabling = changes.get("is_active") is False
    # Losing management means either being switched to a role without it, or
    # being switched off entirely.
    losing_management = can_manage_people(user) and (
        disabling or (new_role is not None and not _role_grants_management(new_role))
    )

    if losing_management:
        if user.id == acting_user_id:
            raise ConflictError(
                "You cannot remove your own access to manage people. "
                "Ask another administrator to do it."
            )
        if await _other_managers(session, excluding=user.id) == 0:
            raise ConflictError(
                "This is the only account that can manage people. "
                "Give someone else the admin role first."
            )

    if "full_name" in changes:
        user.full_name = changes["full_name"]
    if new_role is not None:
        user.roles = [new_role]
    if "is_active" in changes:
        user.is_active = bool(changes["is_active"])

    await session.flush()

    # Ends their ability to obtain a *new* access token. On its own this does
    # not stop the token they already hold - permissions are signed into it -
    # which is why the admin router additionally re-reads the account per
    # request (`api.deps.assert_access_not_withdrawn`). The two together are
    # what make a demotion or a disable bite immediately.
    if new_role is not None or "is_active" in changes:
        await service.revoke_all_for_user(session, user.id)

    await session.refresh(user)
    return user


async def reset_password(session: AsyncSession, *, user: User) -> str:
    """Mint a new password. Returns it once; it is never readable again."""
    password = generate_password()
    # set_password also revokes every session for the account: otherwise a
    # stolen one would survive the very act meant to end it.
    await service.set_password(session, user=user, password=password)
    return password
