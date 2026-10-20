"""Dashboard endpoints for reviewing data requests.

A decision is final: approve and decline act only on a pending request. What
can be repeated is the email, through resend, for when mail failed or a link
ran out.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from wascat.core.deps import SessionDep, require_permission
from wascat.core.envelope import CanonicalJSONResponse, envelope
from wascat.core.security.tokens import AccessClaims
from wascat.domains.audit import service as audit
from wascat.domains.iam.models import REQUEST_REVIEW
from wascat.domains.requests import service
from wascat.domains.requests.models import DataRequestStatus
from wascat.domains.requests.schemas import DataRequestApprove, DataRequestDecline

router = APIRouter(prefix="/data-requests", tags=["admin:requests"])

CanReview = Annotated[AccessClaims, Depends(require_permission(REQUEST_REVIEW))]


def _actor(request: Request, claims: AccessClaims) -> dict[str, Any]:
    return {
        "actor_id": claims.user_id,
        "actor_email": claims.email,
        "request_id": getattr(request.state, "request_id", None),
        "ip": request.client.host if request.client else None,
    }


@router.get("", summary="Data requests, newest first")
async def list_requests(
    request: Request, db: SessionDep, claims: CanReview, status: DataRequestStatus | None = None
) -> CanonicalJSONResponse:
    rows = await service.list_requests(db, status)
    return envelope(
        request,
        [service.snapshot(row) for row in rows],
        meta={"count": len(rows), "pending": await service.pending_count(db)},
    )


@router.get("/{request_id}", summary="One data request")
async def get_request(
    request_id: uuid.UUID, request: Request, db: SessionDep, claims: CanReview
) -> CanonicalJSONResponse:
    return envelope(request, service.snapshot(await service.get_request_or_404(db, request_id)))


@router.post(
    "/{request_id}/approve", summary="Approve, and mail the requester a private download link"
)
async def approve(
    request_id: uuid.UUID,
    payload: DataRequestApprove,
    request: Request,
    db: SessionDep,
    claims: CanReview,
) -> CanonicalJSONResponse:
    data_request = await service.get_request_or_404(db, request_id)
    before = service.snapshot(data_request)
    await service.approve(db, data_request, payload, reviewer_id=claims.user_id)
    after = service.snapshot(data_request)
    await audit.record(
        db,
        action="data_request.approve",
        entity_type="data_request",
        entity_id=str(data_request.id),
        before=before,
        after=after,
        summary=f"approved {data_request.reference} for {data_request.email}",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, after)


@router.post("/{request_id}/decline", summary="Decline, and mail the requester the reason")
async def decline(
    request_id: uuid.UUID,
    payload: DataRequestDecline,
    request: Request,
    db: SessionDep,
    claims: CanReview,
) -> CanonicalJSONResponse:
    data_request = await service.get_request_or_404(db, request_id)
    before = service.snapshot(data_request)
    await service.decline(db, data_request, payload, reviewer_id=claims.user_id)
    after = service.snapshot(data_request)
    await audit.record(
        db,
        action="data_request.decline",
        entity_type="data_request",
        entity_id=str(data_request.id),
        before=before,
        after=after,
        summary=f"declined {data_request.reference} for {data_request.email}",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, after)


@router.post(
    "/{request_id}/resend", summary="Mail the decision again (an approval gets a fresh link)"
)
async def resend(
    request_id: uuid.UUID, request: Request, db: SessionDep, claims: CanReview
) -> CanonicalJSONResponse:
    data_request = await service.get_request_or_404(db, request_id)
    before = service.snapshot(data_request)
    await service.resend(db, data_request)
    after = service.snapshot(data_request)
    await audit.record(
        db,
        action="data_request.resend",
        entity_type="data_request",
        entity_id=str(data_request.id),
        before=before,
        after=after,
        summary=f"resent the decision on {data_request.reference} to {data_request.email}",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, after)
