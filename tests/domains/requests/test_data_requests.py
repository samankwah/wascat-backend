"""Data requests: what would be damaging to get wrong.

That a request is never lost to a mail failure, that only reviewers are told,
that the access token is never stored, that a link stops working when it
should, that a requester can only reach the collections they were given, and
that nothing about a submission or a private link is ever cached.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.domains.catalog.test_admin import add_record, make_draft
from tests.domains.iam.test_admin_users import seed_roles
from wascat.core import mail
from wascat.core.errors import ConflictError, InvalidQueryError, NotFoundError
from wascat.core.security.tokens import create_access_token, hash_refresh_token
from wascat.domains.audit.models import AuditEvent
from wascat.domains.catalog import admin_service as catalog_admin
from wascat.domains.catalog.models import Collection
from wascat.domains.iam import service as iam
from wascat.domains.iam.models import REQUEST_REVIEW, User
from wascat.domains.requests import service
from wascat.domains.requests.models import DataRequest, DataRequestStatus
from wascat.domains.requests.schemas import (
    DataRequestApprove,
    DataRequestCreate,
    DataRequestDecline,
)

pytestmark = pytest.mark.db


class Outbox:
    """Stands in for the mailer: records each message, optionally failing."""

    def __init__(self) -> None:
        self.sent: list[mail.Message] = []
        self.failure: str | None = None

    async def send(self, message: mail.Message) -> str | None:
        if self.failure:
            return self.failure
        self.sent.append(message)
        return None

    def to(self, address: str) -> list[mail.Message]:
        return [message for message in self.sent if address in message.to]


class SigningStore:
    """Presigns predictably, so a test can see which key and lifetime it was asked for."""

    async def presign_get(self, key: str, *, expires: int) -> str:
        return f"https://signed.example/{key}?expires={expires}"


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    box = Outbox()
    monkeypatch.setattr(mail, "send", box.send)
    return box


@pytest.fixture
async def collection(session: AsyncSession) -> Collection:
    collection, release = await make_draft(session)
    for frame in range(1, 4):
        await add_record(session, collection, release, frame=frame)
    await catalog_admin.publish_release(session, release)
    return collection


@pytest.fixture
async def people(session: AsyncSession) -> dict[str, User]:
    await seed_roles(session)
    return {
        role: await iam.create_user(
            session,
            email=f"{role}-{uuid.uuid4().hex[:6]}@example.org",
            password="x" * 16,
            role_slugs=[role],
        )
        for role in ("admin", "curator", "viewer")
    }


def ask(collection: Collection, **overrides: object) -> DataRequestCreate:
    fields: dict[str, object] = {
        "name": "Ama Mensah",
        "email": "ama@university.example.org",
        "organisation": "University of Ghana",
        "purpose": "Training a cloud segmentation model for my thesis.",
        "collections": [collection.slug],
        "acceptLicence": True,
        **overrides,
    }
    return DataRequestCreate.model_validate(fields)


async def submit(
    session: AsyncSession, collection: Collection, ip: str = "203.0.113.5", **overrides: object
) -> DataRequest:
    received = await service.create_request(session, ask(collection, **overrides), ip=ip)
    assert received.request is not None
    return received.request


class TestAsking:
    async def test_a_request_is_stored_and_reviewers_and_requester_are_told(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        request = await submit(session, collection)
        await service.announce(session, request)

        assert request.status is DataRequestStatus.PENDING
        assert request.reference.startswith("DR-")
        assert len(request.reference) == 8
        # Only accounts that can act on it: the admin, not the curator or viewer.
        assert outbox.to(people["admin"].email)
        assert not outbox.to(people["curator"].email)
        assert not outbox.to(people["viewer"].email)
        assert request.reference in outbox.to("ama@university.example.org")[0].subject
        assert request.admins_notified_at is not None

    async def test_a_mail_failure_never_loses_the_request(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        outbox.failure = "mail service returned 500"
        request = await submit(session, collection)
        await service.announce(session, request)

        stored = await session.get(DataRequest, request.id)
        assert stored is not None
        assert stored.last_mail_error == "mail service returned 500"
        assert stored.admins_notified_at is None

    async def test_a_bot_filling_the_hidden_field_is_told_it_worked_and_nothing_is_kept(
        self, session: AsyncSession, collection: Collection
    ) -> None:
        received = await service.create_request(
            session, ask(collection, website="https://spam.example"), ip=None
        )
        assert received.request is None
        assert received.reference.startswith("DR-")
        assert (await session.execute(select(DataRequest))).first() is None

    async def test_unknown_collections_and_an_unaccepted_licence_are_refused(
        self, session: AsyncSession, collection: Collection
    ) -> None:
        with pytest.raises(InvalidQueryError) as unknown:
            await service.create_request(
                session, ask(collection, collections=["no-such-thing"]), ip=None
            )
        assert "collections" in (unknown.value.details or {})
        with pytest.raises(InvalidQueryError) as licence:
            await service.create_request(session, ask(collection, acceptLicence=False), ip=None)
        assert "acceptLicence" in (licence.value.details or {})

    def test_the_form_insists_on_a_real_purpose_and_address(self, collection: Collection) -> None:
        with pytest.raises(ValidationError):
            ask(collection, purpose="data pls")
        with pytest.raises(ValidationError):
            ask(collection, email="not-an-address")
        with pytest.raises(ValidationError):
            ask(collection, collections=[])

    async def test_one_address_is_limited_per_day(
        self, session: AsyncSession, collection: Collection
    ) -> None:
        for index in range(service.PER_EMAIL_PER_DAY):
            await submit(session, collection, ip=f"198.51.100.{index}")
        with pytest.raises(service.TooManyRequestsError):
            # Capitals do not make it a different address.
            await submit(
                session, collection, ip="198.51.100.99", email="AMA@university.example.org"
            )

    async def test_one_network_address_is_limited_per_hour(
        self, session: AsyncSession, collection: Collection
    ) -> None:
        for index in range(service.PER_IP_PER_HOUR):
            await submit(session, collection, email=f"person{index}@example.org")
        with pytest.raises(service.TooManyRequestsError):
            await submit(session, collection, email="another@example.org")


class TestDeciding:
    async def test_approval_mails_a_link_whose_token_is_never_stored(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        request = await submit(session, collection)
        await service.approve(
            session, request, DataRequestApprove(note="Enjoy."), reviewer_id=people["admin"].id
        )

        (message,) = outbox.to(request.email)
        token = message.text.split("/data/", 1)[1].split()[0]
        assert request.status is DataRequestStatus.APPROVED
        assert request.access_token_hash == hash_refresh_token(token) != token
        assert token not in (request.access_token_hash or "")
        assert request.access_expires_at is not None
        assert (
            timedelta(days=6, hours=23)
            < request.access_expires_at - datetime.now(UTC)
            <= timedelta(days=7)
        )
        assert "Enjoy." in message.text
        assert request.requester_notified_at is not None

    async def test_a_decision_is_final(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        request = await submit(session, collection)
        await service.approve(
            session, request, DataRequestApprove(), reviewer_id=people["admin"].id
        )
        with pytest.raises(ConflictError):
            await service.decline(
                session,
                request,
                DataRequestDecline(note="Changed my mind"),
                reviewer_id=people["admin"].id,
            )

    async def test_a_decline_needs_a_reason_and_mails_it(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        with pytest.raises(ValidationError):
            DataRequestDecline(note="")
        request = await submit(session, collection)
        await service.decline(
            session,
            request,
            DataRequestDecline(note="Commercial use is not covered."),
            reviewer_id=people["admin"].id,
        )
        assert request.status is DataRequestStatus.DECLINED
        assert request.access_token_hash is None
        assert "Commercial use is not covered." in outbox.to(request.email)[0].text

    async def test_resending_an_approval_replaces_the_link(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        request = await submit(session, collection)
        await service.approve(
            session, request, DataRequestApprove(), reviewer_id=people["admin"].id
        )
        first = outbox.sent[-1].text.split("/data/", 1)[1].split()[0]
        await service.resend(session, request)
        second = outbox.sent[-1].text.split("/data/", 1)[1].split()[0]

        assert first != second
        assert await service.request_for_token(session, second) is request
        with pytest.raises(NotFoundError):
            await service.request_for_token(session, first)


class TestReachingTheData:
    async def approved(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> tuple[DataRequest, str]:
        request = await submit(session, collection)
        await service.approve(
            session, request, DataRequestApprove(), reviewer_id=people["admin"].id
        )
        return request, outbox.sent[-1].text.split("/data/", 1)[1].split()[0]

    async def test_a_valid_link_lists_its_collections(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        _request, token = await self.approved(session, collection, people, outbox)
        summary = await service.access_summary(
            session, await service.request_for_token(session, token)
        )
        (entry,) = summary["collections"]
        assert entry["slug"] == collection.slug
        assert entry["records"] == 3
        assert entry["release"] == "1.0"

    async def test_an_expired_link_is_gone_and_an_unknown_one_is_not_found(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        request, token = await self.approved(session, collection, people, outbox)
        request.access_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        with pytest.raises(service.AccessGoneError):
            await service.request_for_token(session, token)
        with pytest.raises(NotFoundError):
            await service.request_for_token(session, "made-up-token")

    async def test_files_carry_short_lived_signed_links_and_stay_within_the_request(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        request, _ = await self.approved(session, collection, people, outbox)
        store = SigningStore()
        files, total, next_cursor = await service.file_page(
            session, store, request, collection.slug, None
        )  # type: ignore[arg-type]
        assert total == 3
        assert next_cursor is None
        assert len(files) == 3
        assert all(entry["url"].endswith(f"expires={service.FILE_LINK_SECONDS}") for entry in files)

        other, _ = await make_draft(session)
        with pytest.raises(NotFoundError):
            await service.file_page(session, store, request, other.slug, None)  # type: ignore[arg-type]

    async def test_metadata_and_manifest_cover_every_record(
        self, session: AsyncSession, collection: Collection, people: dict[str, User], outbox: Outbox
    ) -> None:
        request, _ = await self.approved(session, collection, people, outbox)
        csv_text = await service.metadata_csv(session, request, collection.slug)
        lines = csv_text.strip().splitlines()
        assert lines[0].startswith("id,")
        assert len(lines) == 4
        manifest = await service.manifest(session, SigningStore(), request, collection.slug)  # type: ignore[arg-type]
        assert len(manifest.splitlines()) == 3
        assert all(
            line.endswith(f"expires={service.MANIFEST_LINK_SECONDS}")
            for line in manifest.splitlines()
        )


class TestHttpSurface:
    @pytest.fixture
    async def client(self, session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
        from wascat.core.db import get_session
        from wascat.main import app

        async def override() -> AsyncIterator[AsyncSession]:
            yield session

        app.dependency_overrides[get_session] = override
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://wascat.example.org"
        ) as http:
            yield http
        app.dependency_overrides.clear()

    @staticmethod
    def bearer(user: User, permissions: set[str]) -> dict[str, str]:
        token, _ = create_access_token(
            user_id=user.id,
            email=user.email,
            roles={role.slug for role in user.roles},
            permissions=permissions,
        )
        return {"Authorization": f"Bearer {token}"}

    async def test_a_submission_is_created_and_never_cached(
        self,
        client: httpx.AsyncClient,
        collection: Collection,
        people: dict[str, User],
        outbox: Outbox,
    ) -> None:
        body = ask(collection).model_dump(by_alias=True)
        body["email"] = str(body["email"])
        created = await client.post("/api/v1/data-requests", json=body)
        assert created.status_code == 201, created.text
        assert created.json()["data"]["reference"].startswith("DR-")
        assert created.headers["cache-control"] == "no-store"

        refused = await client.post("/api/v1/data-requests", json={**body, "purpose": "short"})
        assert refused.status_code == 400
        assert refused.headers["cache-control"] == "no-store"
        assert "purpose" in refused.json()["error"]["details"]

    async def test_private_links_are_never_cached_even_when_wrong(
        self, client: httpx.AsyncClient
    ) -> None:
        response = await client.get("/api/v1/data-requests/access/not-a-real-token")
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"

    async def test_only_reviewers_see_requests_and_a_decision_is_audited(
        self,
        client: httpx.AsyncClient,
        session: AsyncSession,
        collection: Collection,
        people: dict[str, User],
        outbox: Outbox,
    ) -> None:
        request = await submit(session, collection)
        viewer = self.bearer(people["viewer"], {"catalog:read", "audit:read"})
        assert (await client.get("/api/v1/admin/data-requests", headers=viewer)).status_code == 403

        admin = self.bearer(people["admin"], {REQUEST_REVIEW})
        listing = await client.get("/api/v1/admin/data-requests?status=PENDING", headers=admin)
        assert listing.status_code == 200
        assert listing.json()["meta"]["pending"] == 1
        assert "accessTokenHash" not in listing.json()["data"][0]

        approved = await client.post(
            f"/api/v1/admin/data-requests/{request.id}/approve", json={}, headers=admin
        )
        assert approved.status_code == 200
        assert approved.json()["data"]["status"] == "APPROVED"
        again = await client.post(
            f"/api/v1/admin/data-requests/{request.id}/approve", json={}, headers=admin
        )
        assert again.status_code == 409

        events = (
            (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.entity_id == str(request.id))
                )
            )
            .scalars()
            .all()
        )
        assert [event.action for event in events] == ["data_request.approve"]
