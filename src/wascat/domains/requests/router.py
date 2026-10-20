"""Public endpoints: asking for data, and reaching it once approved.

Nothing here is cached. The middleware marks every response under
/api/v1/data-requests ``no-store``, errors included: a submission must never be
replayed from a cache, and an approved requester's links are theirs alone.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from wascat.core.deps import SessionDep
from wascat.core.envelope import CanonicalJSONResponse, envelope
from wascat.domains.requests import service
from wascat.domains.requests.schemas import DataRequestCreate
from wascat.storage.deps import StoreDep

router = APIRouter(prefix="/api/v1/data-requests", tags=["data requests"])


def client_ip(request: Request) -> str | None:
    """The requester's address, for the rate limit.

    Behind the frontend's proxy and the platform's, the socket peer is a proxy;
    the first X-Forwarded-For entry is the browser. It can be forged, which is
    acceptable for a soft limit backed by a per-address one.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",", 1)[0].strip()
    if first:
        return first
    return request.client.host if request.client else None


@router.post("", status_code=201, summary="Ask for a collection's data")
async def create_request(
    payload: DataRequestCreate, request: Request, db: SessionDep
) -> CanonicalJSONResponse:
    received = await service.create_request(db, payload, ip=client_ip(request))
    if received.request is not None:
        # Saved before anything is mailed, so a mail outage cannot lose a request.
        await db.commit()
        await service.announce(db, received.request)
        await db.commit()
    return envelope(request, {"reference": received.reference}, status_code=201)


@router.get("/access/{token}", summary="What an approved request can download")
async def access(token: str, request: Request, db: SessionDep) -> CanonicalJSONResponse:
    data_request = await service.request_for_token(db, token)
    return envelope(request, await service.access_summary(db, data_request))


@router.get("/access/{token}/collections/{slug}/files", summary="A page of a collection's files")
async def files(
    token: str,
    slug: str,
    request: Request,
    db: SessionDep,
    store: StoreDep,
    cursor: str | None = None,
) -> CanonicalJSONResponse:
    data_request = await service.request_for_token(db, token)
    entries, total, next_cursor = await service.file_page(db, store, data_request, slug, cursor)
    return envelope(
        request, entries, meta={"count": len(entries), "total": total, "nextCursor": next_cursor}
    )


def _attachment(body: str, filename: str, media_type: str) -> PlainTextResponse:
    return PlainTextResponse(
        body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/access/{token}/collections/{slug}/metadata.json", summary="Every record's metadata, as JSON"
)
async def metadata_json(
    token: str, slug: str, request: Request, db: SessionDep
) -> CanonicalJSONResponse:
    data_request = await service.request_for_token(db, token)
    records = await service.metadata_json(db, data_request, slug)
    response = envelope(request, records, meta={"count": len(records)})
    response.headers["Content-Disposition"] = f'attachment; filename="{slug}-metadata.json"'
    return response


@router.get(
    "/access/{token}/collections/{slug}/metadata.csv", summary="Every record's metadata, as CSV"
)
async def metadata_csv(token: str, slug: str, db: SessionDep) -> PlainTextResponse:
    data_request = await service.request_for_token(db, token)
    return _attachment(
        await service.metadata_csv(db, data_request, slug), f"{slug}-metadata.csv", "text/csv"
    )


@router.get(
    "/access/{token}/collections/{slug}/manifest.txt", summary="Every file's link, one per line"
)
async def manifest(token: str, slug: str, db: SessionDep, store: StoreDep) -> PlainTextResponse:
    data_request = await service.request_for_token(db, token)
    return _attachment(
        await service.manifest(db, store, data_request, slug), f"{slug}-files.txt", "text/plain"
    )
