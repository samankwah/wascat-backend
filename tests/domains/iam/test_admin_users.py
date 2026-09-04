"""Managing accounts from the dashboard.

The things that would be damaging to get wrong, asserted rather than assumed:
that a change of role takes effect immediately rather than whenever a token
expires, that an administrator cannot strand themselves or the archive, that
the only account able to manage people cannot be removed, and that no password
material reaches a response body or the audit log.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.api.deps import assert_access_not_withdrawn
from wascat.core.errors import ConflictError, NotFoundError, UnauthorizedError
from wascat.core.security.tokens import AccessClaims, decode_access_token
from wascat.domains.audit.models import AuditEvent
from wascat.domains.iam import admin_service, service
from wascat.domains.iam.admin_schemas import UserCreate, UserUpdate
from wascat.domains.iam.models import (
    ROLE_PERMISSIONS,
    USER_MANAGE,
    Permission,
    RefreshSession,
    Role,
    User,
)

pytestmark = pytest.mark.db


async def seed_roles(session: AsyncSession) -> None:
    """Roles *with* their permissions.

    The migration seeds these; a test that runs after a downgrade may not see
    them. The permissions matter here in a way they do not for sign-in tests:
    every guard in admin_service counts the `user:manage` permission rather
    than the role slug, so a role without its permissions would make the
    last-administrator check vacuous and the tests pass for the wrong reason.
    """
    known = {
        permission.slug: permission
        for permission in (await session.execute(select(Permission))).scalars().all()
    }
    for slug in {slug for slugs in ROLE_PERMISSIONS.values() for slug in slugs}:
        if slug not in known:
            known[slug] = Permission(slug=slug, description=slug)
            session.add(known[slug])
    await session.flush()

    existing = {role.slug: role for role in (await session.execute(select(Role))).scalars().all()}
    for slug, permissions in ROLE_PERMISSIONS.items():
        role = existing.get(slug) or Role(slug=slug, system=True)
        session.add(role)
        role.permissions = [known[name] for name in permissions]
    await session.flush()


async def live_sessions(session: AsyncSession, user: User) -> list[RefreshSession]:
    """Refresh sessions still open for an account."""
    rows = await session.execute(
        select(RefreshSession).where(
            RefreshSession.user_id == user.id,
            RefreshSession.revoked_at.is_(None),
        )
    )
    return list(rows.scalars().all())


def _request_with(claims: AccessClaims) -> Any:
    """The one thing `assert_access_not_withdrawn` reads off a request.

    A stand-in rather than a real Request: the dependency only ever touches
    `request.state.claims`, and building a full ASGI scope to hand it one would
    obscure what is being tested.
    """
    return SimpleNamespace(state=SimpleNamespace(claims=claims))


async def make_user(session: AsyncSession, role: str, *, active: bool = True) -> User:
    user = await service.create_user(
        session,
        email=f"{role}-{uuid.uuid4().hex[:8]}@wascat.test",
        password="correct-horse-battery-staple",
        full_name=f"Test {role}",
        role_slugs=[role],
    )
    user.is_active = active
    await session.flush()
    return user


@pytest.fixture
async def admin(session: AsyncSession) -> User:
    await seed_roles(session)
    return await make_user(session, "admin")


class TestCreate:
    async def test_invites_with_a_generated_password(
        self, session: AsyncSession, admin: User
    ) -> None:
        user, password = await admin_service.create_user(
            session, UserCreate(email="new@wascat.test", fullName="New Person", role="curator")
        )
        assert user.email == "new@wascat.test"
        assert [role.slug for role in user.roles] == ["curator"]
        # Strong enough that no complexity rule is needed, and not derived from
        # anything the caller sent.
        assert len(password) >= 20

        # The password works, which is the only thing that proves it was the
        # one actually stored.
        await session.commit()
        opened = await service.authenticate(session, email=user.email, password=password)
        assert opened.user.id == user.id

    async def test_refuses_a_duplicate_address(self, session: AsyncSession, admin: User) -> None:
        with pytest.raises(ConflictError) as refused:
            await admin_service.create_user(session, UserCreate(email=admin.email, role="curator"))
        assert "already has an account" in str(refused.value)

    async def test_refuses_an_unknown_role(self, session: AsyncSession, admin: User) -> None:
        with pytest.raises(NotFoundError) as refused:
            await admin_service.create_user(
                session, UserCreate(email="nobody@wascat.test", role="superuser")
            )
        # The message lists what is available rather than only what is wrong.
        assert "admin" in str(refused.value)

    async def test_the_snapshot_carries_no_password_material(
        self, session: AsyncSession, admin: User
    ) -> None:
        user, _ = await admin_service.create_user(
            session, UserCreate(email="quiet@wascat.test", role="viewer")
        )
        snapshot = admin_service.snapshot(user)
        assert "password" not in snapshot
        assert "password_hash" not in snapshot
        assert "passwordHash" not in snapshot


class TestRoleChangesTakeEffectNow:
    async def test_changing_a_role_revokes_that_account_s_sessions(
        self, session: AsyncSession, admin: User
    ) -> None:
        curator = await make_user(session, "curator")
        await session.commit()

        opened = await service.authenticate(
            session, email=curator.email, password="correct-horse-battery-staple"
        )
        assert opened.refresh_token

        await admin_service.update_user(
            session,
            user=curator,
            payload=UserUpdate(role="viewer"),
            acting_user_id=admin.id,
        )
        await session.commit()

        # Permissions ride in a signed token, so without this the demotion
        # would not bite until the access token expired.
        live = await live_sessions(session, curator)
        assert live == []

    async def test_disabling_revokes_sessions_and_blocks_sign_in(
        self, session: AsyncSession, admin: User
    ) -> None:
        curator = await make_user(session, "curator")
        await session.commit()
        await service.authenticate(
            session, email=curator.email, password="correct-horse-battery-staple"
        )

        await admin_service.update_user(
            session,
            user=curator,
            payload=UserUpdate(isActive=False),
            acting_user_id=admin.id,
        )
        await session.commit()

        live = await live_sessions(session, curator)
        assert live == []

        # A distinct error from a wrong password: someone whose access was
        # withdrawn should be told that, not left retrying a password that was
        # never the problem.
        with pytest.raises(service.AccountDisabledError):
            await service.authenticate(
                session, email=curator.email, password="correct-horse-battery-staple"
            )

    async def test_a_withdrawn_permission_is_refused_on_the_next_request(
        self, session: AsyncSession
    ) -> None:
        """The token keeps its old claims; the request must still be refused.

        Permissions are signed into the access token, so a demotion does not
        change what an already-issued token *says*. Revoking sessions only
        stops a new one being minted. Without the admin router's per-request
        re-read, a demoted curator would keep writing until their token
        expired - up to fifteen minutes.
        """
        await seed_roles(session)
        admin = await make_user(session, "admin")
        curator = await make_user(session, "curator")
        await session.commit()

        opened = await service.authenticate(
            session, email=curator.email, password="correct-horse-battery-staple"
        )
        # The claims as minted, while they were still a curator.
        claims = decode_access_token(opened.access_token)
        assert "vocab:write" in claims.permissions

        await admin_service.update_user(
            session,
            user=curator,
            payload=UserUpdate(role="viewer"),
            acting_user_id=admin.id,
        )
        await session.commit()

        # The same unexpired token, presented after the demotion.
        with pytest.raises(UnauthorizedError) as refused:
            await assert_access_not_withdrawn(_request_with(claims), session)
        assert "access has changed" in str(refused.value)

    async def test_a_disabled_account_is_refused_on_the_next_request(
        self, session: AsyncSession
    ) -> None:
        await seed_roles(session)
        admin = await make_user(session, "admin")
        curator = await make_user(session, "curator")
        await session.commit()

        opened = await service.authenticate(
            session, email=curator.email, password="correct-horse-battery-staple"
        )
        claims = decode_access_token(opened.access_token)

        await admin_service.update_user(
            session,
            user=curator,
            payload=UserUpdate(isActive=False),
            acting_user_id=admin.id,
        )
        await session.commit()

        with pytest.raises(UnauthorizedError) as refused:
            await assert_access_not_withdrawn(_request_with(claims), session)
        assert "no longer active" in str(refused.value)

    async def test_gaining_a_permission_does_not_disrupt_a_session(
        self, session: AsyncSession
    ) -> None:
        """Only *withdrawal* invalidates a session.

        Curator to admin is the one strictly additive promotion here. The roles
        are not a ladder: a viewer holds `audit:read` and a curator does not,
        so promoting a viewer to curator takes something away and is correctly
        treated as a withdrawal.
        """
        await seed_roles(session)
        admin = await make_user(session, "admin")
        curator = await make_user(session, "curator")
        await session.commit()

        opened = await service.authenticate(
            session, email=curator.email, password="correct-horse-battery-staple"
        )
        claims = decode_access_token(opened.access_token)

        await admin_service.update_user(
            session,
            user=curator,
            payload=UserUpdate(role="admin"),
            acting_user_id=admin.id,
        )
        await session.commit()

        # Their existing token still works; nothing was taken away.
        await assert_access_not_withdrawn(_request_with(claims), session)

    async def test_renaming_alone_leaves_sessions_alone(
        self, session: AsyncSession, admin: User
    ) -> None:
        curator = await make_user(session, "curator")
        await session.commit()
        await service.authenticate(
            session, email=curator.email, password="correct-horse-battery-staple"
        )

        await admin_service.update_user(
            session,
            user=curator,
            payload=UserUpdate(fullName="Renamed"),
            acting_user_id=admin.id,
        )
        await session.commit()

        # A name is not an access change; signing someone out for it would be
        # gratuitous.
        live = await live_sessions(session, curator)
        assert len(live) == 1


class TestNobodyGetsStranded:
    async def test_you_cannot_demote_yourself(self, session: AsyncSession, admin: User) -> None:
        # Even with another administrator available: locking yourself out by
        # accident is the mistake worth refusing outright.
        await make_user(session, "admin")
        await session.flush()

        with pytest.raises(ConflictError) as refused:
            await admin_service.update_user(
                session,
                user=admin,
                payload=UserUpdate(role="viewer"),
                acting_user_id=admin.id,
            )
        assert "your own access" in str(refused.value)

    async def test_you_cannot_disable_yourself(self, session: AsyncSession, admin: User) -> None:
        await make_user(session, "admin")
        await session.flush()

        with pytest.raises(ConflictError):
            await admin_service.update_user(
                session,
                user=admin,
                payload=UserUpdate(isActive=False),
                acting_user_id=admin.id,
            )

    async def test_the_only_remaining_manager_cannot_be_removed(
        self, session: AsyncSession, admin: User
    ) -> None:
        # One administrator besides the actor, and the actor is then disabled,
        # leaving exactly one account that can manage people.
        solo = await make_user(session, "admin")
        admin.is_active = False
        await session.flush()

        with pytest.raises(ConflictError) as refused:
            await admin_service.update_user(
                session,
                user=solo,
                payload=UserUpdate(role="curator"),
                acting_user_id=admin.id,
            )
        assert "only account" in str(refused.value)

    async def test_demoting_one_of_two_administrators_is_allowed(
        self, session: AsyncSession, admin: User
    ) -> None:
        spare = await make_user(session, "admin")
        keeper = await make_user(session, "admin")
        await session.flush()
        assert keeper.is_active

        updated = await admin_service.update_user(
            session,
            user=spare,
            payload=UserUpdate(role="curator"),
            acting_user_id=admin.id,
        )
        assert [role.slug for role in updated.roles] == ["curator"]

    async def test_a_curator_losing_their_role_is_never_guarded(
        self, session: AsyncSession, admin: User
    ) -> None:
        # The guards are about `user:manage`, not about seniority. Demoting
        # someone who never had it is ordinary.
        curator = await make_user(session, "curator")
        await session.flush()

        updated = await admin_service.update_user(
            session,
            user=curator,
            payload=UserUpdate(role="viewer"),
            acting_user_id=admin.id,
        )
        assert [role.slug for role in updated.roles] == ["viewer"]
        assert USER_MANAGE not in updated.permissions


class TestPasswordReset:
    async def test_the_new_password_works_and_the_old_one_does_not(
        self, session: AsyncSession, admin: User
    ) -> None:
        curator = await make_user(session, "curator")
        await session.commit()

        issued = await admin_service.reset_password(session, user=curator)
        await session.commit()

        opened = await service.authenticate(session, email=curator.email, password=issued)
        assert opened.user.id == curator.id

        with pytest.raises(service.InvalidCredentialsError):
            await service.authenticate(
                session, email=curator.email, password="correct-horse-battery-staple"
            )

    async def test_a_reset_ends_every_existing_session(
        self, session: AsyncSession, admin: User
    ) -> None:
        curator = await make_user(session, "curator")
        await session.commit()
        await service.authenticate(
            session, email=curator.email, password="correct-horse-battery-staple"
        )

        await admin_service.reset_password(session, user=curator)
        await session.commit()

        # Otherwise a stolen session survives the very act meant to end it.
        live = await live_sessions(session, curator)
        assert live == []

    async def test_two_resets_never_produce_the_same_password(
        self, session: AsyncSession, admin: User
    ) -> None:
        curator = await make_user(session, "curator")
        await session.commit()
        first = await admin_service.reset_password(session, user=curator)
        second = await admin_service.reset_password(session, user=curator)
        assert first != second


class TestAuditTrail:
    async def test_password_material_never_reaches_the_log(
        self, session: AsyncSession, admin: User
    ) -> None:
        from wascat.domains.audit import service as audit

        user, password = await admin_service.create_user(
            session, UserCreate(email="logged@wascat.test", role="curator")
        )
        await audit.record(
            session,
            action="user.create",
            entity_type="user",
            entity_id=str(user.id),
            after={**admin_service.snapshot(user), "password_hash": user.password_hash},
            actor_id=admin.id,
            actor_email=admin.email,
        )
        await session.commit()

        rows = await session.execute(select(AuditEvent).where(AuditEvent.entity_id == str(user.id)))
        event = rows.scalars().one()
        assert event.after is not None
        # scrub() redacts it even when a caller passes it in by mistake.
        assert event.after["password_hash"] == "[redacted]"
        assert password not in str(event.after)
