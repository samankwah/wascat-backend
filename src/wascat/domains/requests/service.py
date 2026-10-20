"""The rules of a data request: who may ask how often, who is told, how a
decision is made, and what an approved requester can reach.

The routers only shape HTTP and write the audit trail; everything that must
hold whichever way a request arrives lives here.
"""

from __future__ import annotations

import csv
import io
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core import mail
from wascat.core.config import get_settings
from wascat.core.errors import AppError, ConflictError, InvalidQueryError, NotFoundError
from wascat.core.jsformat import iso_z
from wascat.core.pagination import Cursor, decode_cursor
from wascat.core.security.tokens import hash_refresh_token
from wascat.domains.catalog import repository
from wascat.domains.catalog.models import Collection
from wascat.domains.catalog.query import ImageQuery
from wascat.domains.catalog.service import render_record
from wascat.domains.iam.models import REQUEST_REVIEW, Permission, Role, User
from wascat.domains.requests import emails
from wascat.domains.requests.models import DataRequest, DataRequestStatus
from wascat.domains.requests.schemas import (
    DataRequestApprove,
    DataRequestCreate,
    DataRequestDecline,
)
from wascat.storage.base import ObjectStore

#: At most this many requests from one address in a day...
PER_EMAIL_PER_DAY = 3
#: ...and from one network address in an hour.
PER_IP_PER_HOUR = 10

#: How long a file link on the download page works. Short, because the page
#: mints fresh ones every time it is opened while access lasts.
FILE_LINK_SECONDS = 60 * 60
#: How long the links in a manifest work: long enough for a large `wget -i`.
MANIFEST_LINK_SECONDS = 24 * 60 * 60

FILES_PAGE_SIZE = 50
EXPORT_PAGE_SIZE = 100

# No 0/O or 1/I/L, so a reference read aloud or retyped from an email survives.
REFERENCE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
TOKEN_BYTES = 32


class TooManyRequestsError(AppError):
    status_code = 429
    code = "too_many_requests"
    message = "Too many requests from this address. Please try again later."


class AccessGoneError(AppError):
    status_code = 410
    code = "link_expired"
    message = "This download link has expired. Reply to the approval email to ask for a new one."


@dataclass(frozen=True, slots=True)
class Received:
    """What the public endpoint reports. ``request`` is None for a discarded bot submission."""

    reference: str
    request: DataRequest | None


def _now() -> datetime:
    return datetime.now(UTC)


def _new_reference() -> str:
    return "DR-" + "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(5))


# -- Asking ------------------------------------------------------------------


async def create_request(
    session: AsyncSession, payload: DataRequestCreate, *, ip: str | None
) -> Received:
    # Filled in only by something that read the HTML rather than the page.
    # It is told it succeeded, so it learns nothing about what gave it away.
    if payload.website:
        return Received(reference=_new_reference(), request=None)

    if not payload.accept_licence:
        raise InvalidQueryError(
            "Please agree to cite the archive and follow the licence.",
            details={"acceptLicence": ["Please agree to cite the archive and follow the licence."]},
        )

    slugs = list(dict.fromkeys(payload.collections))
    known = set(
        (await session.execute(select(Collection.slug).where(Collection.slug.in_(slugs)))).scalars()
    )
    if unknown := [slug for slug in slugs if slug not in known]:
        raise InvalidQueryError(
            "Unknown collection.",
            details={"collections": [f"No collection named {', '.join(unknown)}."]},
        )

    now = _now()
    recent_from_email = (
        await session.execute(
            select(func.count())
            .select_from(DataRequest)
            .where(
                DataRequest.email == payload.email, DataRequest.created_at > now - timedelta(days=1)
            )
        )
    ).scalar_one()
    if recent_from_email >= PER_EMAIL_PER_DAY:
        raise TooManyRequestsError()
    if ip:
        recent_from_ip = (
            await session.execute(
                select(func.count())
                .select_from(DataRequest)
                .where(DataRequest.ip == ip, DataRequest.created_at > now - timedelta(hours=1))
            )
        ).scalar_one()
        if recent_from_ip >= PER_IP_PER_HOUR:
            raise TooManyRequestsError()

    reference = _new_reference()
    while (
        await session.execute(select(DataRequest.id).where(DataRequest.reference == reference))
    ).first():
        reference = _new_reference()

    request = DataRequest(
        reference=reference,
        name=payload.name,
        email=str(payload.email),
        organisation=payload.organisation,
        purpose=payload.purpose,
        collections=slugs,
        ip=ip,
    )
    session.add(request)
    await session.flush()
    return Received(reference=reference, request=request)


async def reviewer_addresses(session: AsyncSession) -> list[str]:
    """Everyone who can act on a request: active accounts holding request:review."""
    rows = await session.execute(
        select(User.email)
        .join(User.roles)
        .join(Role.permissions)
        .where(User.is_active.is_(True), Permission.slug == REQUEST_REVIEW)
        .distinct()
    )
    return sorted(str(email) for email in rows.scalars())


def _note_failure(request: DataRequest, failure: str | None) -> None:
    if failure:
        request.last_mail_error = failure


async def announce(session: AsyncSession, request: DataRequest) -> None:
    """Tell the reviewers, and tell the requester it arrived."""
    recipients = await reviewer_addresses(session)
    failure = await mail.send(emails.admin_notification(request, recipients))
    if not failure and recipients:
        request.admins_notified_at = _now()
    _note_failure(request, failure)

    failure = await mail.send(emails.acknowledgement(request))
    _note_failure(request, failure)
    await session.flush()


# -- Deciding ----------------------------------------------------------------


async def list_requests(
    session: AsyncSession, status: DataRequestStatus | None
) -> list[DataRequest]:
    stmt = select(DataRequest).order_by(DataRequest.created_at.desc())
    if status is not None:
        stmt = stmt.where(DataRequest.status == status)
    return list((await session.execute(stmt)).scalars())


async def pending_count(session: AsyncSession) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(DataRequest)
            .where(DataRequest.status == DataRequestStatus.PENDING)
        )
    ).scalar_one()


async def get_request_or_404(session: AsyncSession, request_id: uuid.UUID) -> DataRequest:
    request = await session.get(DataRequest, request_id)
    if request is None:
        raise NotFoundError("No such data request.")
    return request


def _assert_pending(request: DataRequest) -> None:
    if request.status != DataRequestStatus.PENDING:
        raise ConflictError(f"This request has already been {request.status.lower()}.")


def _grant_access(request: DataRequest, days: int) -> str:
    """Mint a fresh link. Returns the token, which is never stored."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    request.access_token_hash = hash_refresh_token(token)
    request.access_expires_at = _now() + timedelta(days=days)
    return token


async def approve(
    session: AsyncSession,
    request: DataRequest,
    payload: DataRequestApprove,
    *,
    reviewer_id: uuid.UUID,
) -> DataRequest:
    _assert_pending(request)
    request.status = DataRequestStatus.APPROVED
    request.reviewed_at = _now()
    request.reviewed_by = reviewer_id
    request.decision_note = payload.note or None
    token = _grant_access(request, payload.access_days or get_settings().data_access_days)
    await _send_decision(request, token)
    await session.flush()
    return request


async def decline(
    session: AsyncSession,
    request: DataRequest,
    payload: DataRequestDecline,
    *,
    reviewer_id: uuid.UUID,
) -> DataRequest:
    _assert_pending(request)
    request.status = DataRequestStatus.DECLINED
    request.reviewed_at = _now()
    request.reviewed_by = reviewer_id
    request.decision_note = payload.note
    await _send_decision(request, None)
    await session.flush()
    return request


async def resend(session: AsyncSession, request: DataRequest) -> DataRequest:
    """Mail the decision again. An approval gets a new link, for as long as the first one had."""
    if request.status == DataRequestStatus.PENDING:
        raise ConflictError("This request has not been decided yet, so there is nothing to resend.")
    token = None
    if request.status == DataRequestStatus.APPROVED:
        token = _grant_access(request, get_settings().data_access_days)
    await _send_decision(request, token)
    await session.flush()
    return request


async def _send_decision(request: DataRequest, token: str | None) -> None:
    message = emails.approval(request, token=token) if token else emails.decline(request)
    failure = await mail.send(message)
    if failure:
        request.last_mail_error = failure
    else:
        request.requester_notified_at = _now()
        request.last_mail_error = None


def snapshot(request: DataRequest) -> dict[str, Any]:
    """The dashboard's view of a request. Never includes the token hash."""
    return {
        "id": str(request.id),
        "reference": request.reference,
        "status": request.status.value,
        "name": request.name,
        "email": request.email,
        "organisation": request.organisation,
        "purpose": request.purpose,
        "collections": list(request.collections),
        "createdAt": iso_z(request.created_at),
        "reviewedAt": iso_z(request.reviewed_at) if request.reviewed_at else None,
        "reviewedBy": str(request.reviewed_by) if request.reviewed_by else None,
        "decisionNote": request.decision_note,
        "accessExpiresAt": iso_z(request.access_expires_at) if request.access_expires_at else None,
        "adminsNotifiedAt": iso_z(request.admins_notified_at)
        if request.admins_notified_at
        else None,
        "requesterNotifiedAt": iso_z(request.requester_notified_at)
        if request.requester_notified_at
        else None,
        "lastMailError": request.last_mail_error,
    }


# -- Reaching the data -------------------------------------------------------


async def request_for_token(session: AsyncSession, token: str) -> DataRequest:
    request = (
        await session.execute(
            select(DataRequest).where(DataRequest.access_token_hash == hash_refresh_token(token))
        )
    ).scalar_one_or_none()
    # A declined request never had a token, and a reissued one replaces the
    # old hash, so an unknown token is simply not found.
    if request is None or request.status != DataRequestStatus.APPROVED:
        raise NotFoundError("This download link is not valid.")
    if request.access_expires_at is None or request.access_expires_at <= _now():
        raise AccessGoneError()
    return request


async def _collection_or_404(session: AsyncSession, request: DataRequest, slug: str) -> Collection:
    if slug not in request.collections:
        raise NotFoundError("This collection is not part of the request.")
    collection = await repository.get_collection(session, slug)
    if collection is None:
        raise NotFoundError("This collection no longer exists.")
    return collection


async def access_summary(session: AsyncSession, request: DataRequest) -> dict[str, Any]:
    collections = []
    for slug in request.collections:
        collection = await repository.get_collection(session, slug)
        if collection is None:
            continue
        release = await repository.current_release(session, collection.id)
        page = await repository.list_images(
            session,
            ImageQuery(collection=slug, release=release.version if release else None, limit=1),
            Cursor.start(),
        )
        collections.append(
            {
                "slug": slug,
                "title": collection.title or slug,
                "locationName": collection.location_name,
                "release": release.version if release else None,
                "records": page.total,
                "license": collection.license,
                "citation": collection.citation,
                "doi": collection.doi,
            }
        )
    return {
        "reference": request.reference,
        "name": request.name,
        "approvedAt": iso_z(request.reviewed_at) if request.reviewed_at else None,
        "expiresAt": iso_z(request.access_expires_at) if request.access_expires_at else None,
        "note": request.decision_note,
        "collections": collections,
    }


async def _release_query(session: AsyncSession, collection: Collection, limit: int) -> ImageQuery:
    release = await repository.current_release(session, collection.id)
    return ImageQuery(
        collection=collection.slug,
        release=release.version if release else None,
        sort="oldest",
        limit=limit,
    )


async def _file_entries(
    store: ObjectStore, rows: list[repository.RecordRow], expires: int
) -> list[dict[str, Any]]:
    files = []
    for row in rows:
        for artifact in sorted(row.record.artifacts, key=lambda item: item.type):
            if artifact.type not in ("source", "mask"):
                continue
            files.append(
                {
                    "record": row.record.id,
                    "type": artifact.type,
                    "name": artifact.object_key.rsplit("/", 1)[-1],
                    "objectKey": artifact.object_key,
                    "bytes": artifact.bytes,
                    "checksum": artifact.checksum,
                    "mediaType": artifact.media_type,
                    "url": await store.presign_get(artifact.object_key, expires=expires),
                }
            )
    return files


async def file_page(
    session: AsyncSession, store: ObjectStore, request: DataRequest, slug: str, cursor: str | None
) -> tuple[list[dict[str, Any]], int, str | None]:
    """One page of a collection's files, each with a short-lived link."""
    collection = await _collection_or_404(session, request, slug)
    query = await _release_query(session, collection, FILES_PAGE_SIZE)
    page = await repository.list_images(session, query, decode_cursor(cursor))
    return await _file_entries(store, page.rows, FILE_LINK_SECONDS), page.total, page.next_cursor


async def _all_rows(session: AsyncSession, collection: Collection) -> list[repository.RecordRow]:
    query = await _release_query(session, collection, EXPORT_PAGE_SIZE)
    rows: list[repository.RecordRow] = []
    cursor = Cursor.start()
    while True:
        page = await repository.list_images(session, query, cursor)
        rows.extend(page.rows)
        if not page.next_cursor:
            return rows
        cursor = decode_cursor(page.next_cursor)


async def metadata_json(
    session: AsyncSession, request: DataRequest, slug: str
) -> list[dict[str, Any]]:
    collection = await _collection_or_404(session, request, slug)
    return [render_record(row) for row in await _all_rows(session, collection)]


async def metadata_csv(session: AsyncSession, request: DataRequest, slug: str) -> str:
    """The same records, flattened: every scalar field, one row per record."""
    records = await metadata_json(session, request, slug)
    columns: list[str] = []
    for record in records:
        for key, value in record.items():
            if key not in columns and not isinstance(value, dict | list):
                columns.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({key: record.get(key, "") for key in columns})
    return buffer.getvalue()


async def manifest(
    session: AsyncSession, store: ObjectStore, request: DataRequest, slug: str
) -> str:
    """Every file's link, one per line, for `wget -i` or `curl -K`-style tools."""
    collection = await _collection_or_404(session, request, slug)
    files = await _file_entries(store, await _all_rows(session, collection), MANIFEST_LINK_SECONDS)
    return "".join(f"{entry['url']}\n" for entry in files)
