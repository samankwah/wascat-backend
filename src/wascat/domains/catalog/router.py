"""The public read API.

Six GET endpoints, byte-compatible with the Next.js route handlers they
replace. The responses are built as plain dicts and serialised verbatim rather
than passed through a response model, because the envelope's key set, its
omission of unmeasured fields and its number rendering are all contract; a
Pydantic round-trip would quietly normalise them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.core.db import get_session
from wascat.core.envelope import CanonicalJSONResponse, envelope, public_url
from wascat.core.errors import NotFoundError
from wascat.core.pagination import decode_cursor
from wascat.domains.catalog import repository, service
from wascat.domains.catalog.facets import build_facets
from wascat.domains.catalog.query import parse_image_query
from wascat.domains.catalog.schemas import (
    ERROR_RESPONSES,
    CollectionEnvelope,
    CollectionListEnvelope,
    FacetsEnvelope,
    ImageEnvelope,
    ImagePage,
    ReleaseListEnvelope,
)

router = APIRouter(prefix="/api/v1", tags=["catalog"])

SessionDep = Depends(get_session)


def _with_cursor(request: Request, cursor: str) -> str:
    """The current URL with its cursor replaced, for links.next."""
    params = dict(request.query_params)
    params["cursor"] = cursor
    query = "&".join(f"{key}={value}" for key, value in params.items())
    base = public_url(request).split("?", 1)[0]
    return f"{base}?{query}"


@router.get(
    "/images",
    summary="Search image records",
    responses={200: {"model": ImagePage}, **ERROR_RESPONSES},
)
async def list_images(
    request: Request, session: AsyncSession = SessionDep
) -> CanonicalJSONResponse:
    query = parse_image_query(dict(request.query_params))
    cursor = decode_cursor(query.cursor)
    page = await repository.list_images(session, query, cursor)

    return envelope(
        request,
        [service.render_record(row) for row in page.rows],
        meta={
            "count": len(page.rows),
            "total": page.total,
            "limit": query.limit,
            # Present-but-null on the last page rather than omitted, so a
            # client can test for it without checking key existence.
            "nextCursor": page.next_cursor,
        },
        links={"next": _with_cursor(request, page.next_cursor) if page.next_cursor else None},
    )


@router.get(
    "/images/{record_id}",
    summary="One image record",
    responses={200: {"model": ImageEnvelope}, **ERROR_RESPONSES},
)
async def get_image(
    record_id: str, request: Request, session: AsyncSession = SessionDep
) -> CanonicalJSONResponse:
    row = await repository.get_image(session, record_id)
    if row is None:
        raise NotFoundError("Image record not found")
    return envelope(request, service.render_record_detail(row))


@router.get(
    "/collections",
    summary="All collections",
    responses={200: {"model": CollectionListEnvelope}},
)
async def list_collections(
    request: Request, session: AsyncSession = SessionDep
) -> CanonicalJSONResponse:
    payload = await service.render_collection_summaries(session)
    return envelope(request, payload, meta={"count": len(payload)})


@router.get(
    "/collections/{slug}",
    summary="One collection",
    responses={200: {"model": CollectionEnvelope}, **ERROR_RESPONSES},
)
async def get_collection(
    slug: str, request: Request, session: AsyncSession = SessionDep
) -> CanonicalJSONResponse:
    collection = await repository.get_collection(session, slug)
    if collection is None:
        raise NotFoundError("Collection not found")
    return envelope(request, await service.render_collection(session, collection))


@router.get(
    "/collections/{slug}/releases",
    summary="Release history",
    responses={200: {"model": ReleaseListEnvelope}, **ERROR_RESPONSES},
)
async def list_releases(
    slug: str, request: Request, session: AsyncSession = SessionDep
) -> CanonicalJSONResponse:
    collection = await repository.get_collection(session, slug)
    if collection is None:
        raise NotFoundError("Collection not found")
    payload = await service.render_collection(session, collection)
    releases = payload["releases"]
    return envelope(
        request,
        releases,
        meta={"collection": collection.slug, "count": len(releases)},
    )


@router.get(
    "/facets",
    summary="Filter facets and their counts",
    responses={200: {"model": FacetsEnvelope}},
)
async def get_facets(request: Request, session: AsyncSession = SessionDep) -> CanonicalJSONResponse:
    return envelope(request, await build_facets(session))
