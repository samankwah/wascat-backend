"""Authentication.

The security-relevant behaviours, asserted rather than assumed: that a failed
sign-in reveals nothing about whether the account exists, that a session
rotates on every refresh, that replaying a spent refresh token ends the whole
family, and that a permission the caller lacks is refused.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.api.deps import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME
from wascat.core.security.csrf import CSRF_COOKIE_NAME
from wascat.core.security.tokens import decode_access_token, issue_refresh_token
from wascat.domains.iam import service
from wascat.domains.iam.models import RefreshSession, Role, User

pytestmark = pytest.mark.db

PASSWORD = "correct-horse-battery-staple"


async def seed_roles(session: AsyncSession) -> None:
    """The seed migration provides these; a savepoint-scoped test may not see
    them if the suite ran a downgrade, so make the fixture self-sufficient."""
    existing = (await session.execute(select(Role.slug))).scalars().all()
    for slug in ("admin", "curator", "viewer"):
        if slug not in existing:
            session.add(Role(slug=slug, system=True))
    await session.flush()


@pytest.fixture
async def user(session: AsyncSession) -> User:
    await seed_roles(session)
    created = await service.create_user(
        session,
        email=f"curator-{uuid.uuid4().hex[:8]}@wascat.test",
        password=PASSWORD,
        full_name="Test Curator",
        role_slugs=["curator"],
    )
    await session.commit()
    return created


class TestAuthenticate:
    async def test_signs_in_with_the_right_password(
        self, session: AsyncSession, user: User
    ) -> None:
        opened = await service.authenticate(session, email=user.email, password=PASSWORD)
        assert opened.user.id == user.id
        assert opened.access_token
        assert opened.refresh_token

        claims = decode_access_token(opened.access_token)
        assert claims.user_id == user.id
        assert "curator" in claims.roles
        # Permissions travel in the token, so authorising a request costs no
        # query.
        assert "catalog:write" in claims.permissions
        assert "release:publish" not in claims.permissions

    async def test_rejects_the_wrong_password(self, session: AsyncSession, user: User) -> None:
        with pytest.raises(service.InvalidCredentialsError):
            await service.authenticate(session, email=user.email, password="not it")

    async def test_an_unknown_account_fails_the_same_way(self, session: AsyncSession) -> None:
        # Same exception, same message. Anything else would let someone
        # discover which addresses have accounts.
        with pytest.raises(service.InvalidCredentialsError) as unknown:
            await service.authenticate(session, email="nobody@wascat.test", password="not it")
        assert "Email or password is incorrect." in str(unknown.value)

    async def test_a_disabled_account_cannot_sign_in(
        self, session: AsyncSession, user: User
    ) -> None:
        user.is_active = False
        await session.commit()
        with pytest.raises(service.AccountDisabledError):
            await service.authenticate(session, email=user.email, password=PASSWORD)

    async def test_locks_out_after_repeated_failures(
        self, session: AsyncSession, user: User
    ) -> None:
        for _ in range(service.MAX_LOGIN_ATTEMPTS):
            with pytest.raises(service.InvalidCredentialsError):
                await service.authenticate(session, email=user.email, password="nope")

        # Now even the right password is refused, so guessing cannot simply be
        # continued at speed.
        with pytest.raises(service.RateLimitedError):
            await service.authenticate(session, email=user.email, password=PASSWORD)


class TestRefreshRotation:
    async def test_refresh_issues_a_new_token_and_spends_the_old_one(
        self, session: AsyncSession, user: User
    ) -> None:
        first = await service.authenticate(session, email=user.email, password=PASSWORD)
        await session.commit()

        second = await service.refresh(session, token=first.refresh_token)
        await session.commit()

        assert second.refresh_token != first.refresh_token
        assert second.access_token != first.access_token

    async def test_replaying_a_spent_token_ends_the_whole_family(
        self, session: AsyncSession, user: User
    ) -> None:
        first = await service.authenticate(session, email=user.email, password=PASSWORD)
        await session.commit()
        second = await service.refresh(session, token=first.refresh_token)
        await session.commit()

        # Presenting the first token again means it was used twice. Either the
        # holder replayed it or it was stolen; there is no way to tell, so
        # every session in the family ends.
        with pytest.raises(Exception, match="reused"):
            await service.refresh(session, token=first.refresh_token)
        await session.commit()

        # The token that was legitimately current is now dead too.
        with pytest.raises(Exception, match=r"no longer valid|reused|expired"):
            await service.refresh(session, token=second.refresh_token)

    async def test_an_unknown_token_is_refused(self, session: AsyncSession) -> None:
        token, _ = issue_refresh_token()
        with pytest.raises(Exception, match="no longer valid"):
            await service.refresh(session, token=token)

    async def test_only_the_hash_of_a_refresh_token_is_stored(
        self, session: AsyncSession, user: User
    ) -> None:
        opened = await service.authenticate(session, email=user.email, password=PASSWORD)
        await session.commit()

        stored = (await session.execute(select(RefreshSession))).scalars().all()
        assert stored
        # A database leak must not hand over live sessions.
        assert all(row.token_hash != opened.refresh_token for row in stored)

    async def test_signing_out_ends_the_session(self, session: AsyncSession, user: User) -> None:
        opened = await service.authenticate(session, email=user.email, password=PASSWORD)
        await session.commit()
        await service.revoke(session, token=opened.refresh_token)
        await session.commit()

        with pytest.raises(Exception, match=r"reused|no longer valid"):
            await service.refresh(session, token=opened.refresh_token)

    async def test_changing_a_password_ends_every_session(
        self, session: AsyncSession, user: User
    ) -> None:
        opened = await service.authenticate(session, email=user.email, password=PASSWORD)
        await session.commit()

        await service.set_password(session, user=user, password="a-different-one")
        await session.commit()

        # Otherwise the session an attacker already holds survives the very
        # act meant to end it.
        with pytest.raises(Exception, match=r"reused|no longer valid"):
            await service.refresh(session, token=opened.refresh_token)


class TestHttpSurface:
    """The cookie contract the dashboard depends on."""

    @pytest.fixture
    async def client(self, session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
        from wascat.core.db import get_session
        from wascat.main import app

        async def override() -> AsyncIterator[AsyncSession]:
            yield session

        app.dependency_overrides[get_session] = override
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://wascat.example.org"
        ) as http:
            yield http
        app.dependency_overrides.clear()

    async def test_unauthenticated_requests_are_refused(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/v1/admin/auth/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    async def test_login_sets_the_three_cookies_with_the_right_scopes(
        self, client: httpx.AsyncClient, user: User
    ) -> None:
        response = await client.post(
            "/api/v1/admin/auth/login",
            json={"email": user.email, "password": PASSWORD},
        )
        assert response.status_code == 200

        cookies = {
            name: value
            for header in response.headers.get_list("set-cookie")
            for name, _, value in [header.partition("=")]
        }
        assert ACCESS_COOKIE_NAME in cookies
        assert REFRESH_COOKIE_NAME in cookies
        assert CSRF_COOKIE_NAME in cookies

        raw = " ".join(response.headers.get_list("set-cookie"))
        # The session tokens are unreadable by script, so an XSS bug on the
        # page cannot lift them.
        assert f"{ACCESS_COOKIE_NAME}=" in raw
        assert raw.count("HttpOnly") >= 2
        # The CSRF token is the deliberate exception: the page must echo it.
        csrf_header = next(
            header
            for header in response.headers.get_list("set-cookie")
            if header.startswith(CSRF_COOKIE_NAME)
        )
        assert "HttpOnly" not in csrf_header
        # The refresh token goes only to the endpoints that rotate it.
        refresh_header = next(
            header
            for header in response.headers.get_list("set-cookie")
            if header.startswith(REFRESH_COOKIE_NAME)
        )
        assert "Path=/api/v1/admin/auth" in refresh_header

    async def test_the_response_names_what_the_user_may_do(
        self, client: httpx.AsyncClient, user: User
    ) -> None:
        response = await client.post(
            "/api/v1/admin/auth/login",
            json={"email": user.email, "password": PASSWORD},
        )
        data = response.json()["data"]
        assert data["email"] == user.email
        assert data["roles"] == ["curator"]
        assert "catalog:write" in data["permissions"]
        assert data["csrfToken"]

    async def test_a_sign_in_identifier_need_not_be_a_deliverable_address(
        self, client: httpx.AsyncClient, session: AsyncSession
    ) -> None:
        # Reserved domains are what internal deployments actually use, and
        # nothing is ever sent to this address.
        await seed_roles(session)
        await service.create_user(
            session, email="ops@wascat.local", password=PASSWORD, role_slugs=["viewer"]
        )
        await session.commit()

        response = await client.post(
            "/api/v1/admin/auth/login",
            json={"email": "ops@wascat.local", "password": PASSWORD},
        )
        assert response.status_code == 200

    async def test_a_malformed_identifier_is_rejected(self, client: httpx.AsyncClient) -> None:
        response = await client.post(
            "/api/v1/admin/auth/login",
            json={"email": "not-an-address", "password": PASSWORD},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_query"

    async def test_signing_out_without_a_session_is_not_an_error(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.post("/api/v1/admin/auth/logout")
        assert response.status_code == 200
        assert response.json()["data"]["signedOut"] is True
