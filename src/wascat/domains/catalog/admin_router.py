"""Dashboard endpoints for the catalogue.

Every mutation does three things in one transaction: check the caller may do
it, make the change, and record what changed. Recording inside the transaction
matters - if the change rolls back, so does the claim that it happened.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile

from wascat.core.config import get_settings
from wascat.core.deps import SessionDep, require_permission
from wascat.core.envelope import CanonicalJSONResponse, envelope
from wascat.core.errors import (
    ConflictError,
    PayloadTooLargeError,
    UnprocessableUploadError,
)
from wascat.core.pagination import decode_cursor
from wascat.core.security.tokens import AccessClaims
from wascat.domains.audit import service as audit
from wascat.domains.catalog import admin_service, repository, service, uploads
from wascat.domains.catalog.admin_schemas import (
    BulkImageEdit,
    CollectionCreate,
    CollectionWrite,
    ImageRecordWrite,
    ReleaseCreate,
    ReleaseTransition,
)
from wascat.domains.catalog.models import Release, ReleaseStatus
from wascat.domains.catalog.presenters import release_to_json
from wascat.domains.catalog.query import parse_image_query
from wascat.domains.iam.models import (
    AUDIT_READ,
    CATALOG_READ,
    CATALOG_WRITE,
    RELEASE_PUBLISH,
)
from wascat.storage.deps import StoreDep

router = APIRouter(tags=["admin:catalog"])

CanRead = Annotated[AccessClaims, Depends(require_permission(CATALOG_READ))]
CanWrite = Annotated[AccessClaims, Depends(require_permission(CATALOG_WRITE))]
CanPublish = Annotated[AccessClaims, Depends(require_permission(RELEASE_PUBLISH))]
CanAudit = Annotated[AccessClaims, Depends(require_permission(AUDIT_READ))]


def _actor(request: Request, claims: AccessClaims) -> dict[str, Any]:
    return {
        "actor_id": claims.user_id,
        "actor_email": claims.email,
        "request_id": getattr(request.state, "request_id", None),
        "ip": request.client.host if request.client else None,
    }


def _release_json(release: Release) -> dict[str, Any]:
    payload = release_to_json(release, images=0, total_bytes=0)
    payload.pop("images", None)
    payload.pop("size", None)
    payload["id"] = str(release.id)
    payload["status"] = release.status.value
    payload["retiredAt"] = (
        release.retired_at.isoformat().replace("+00:00", "Z") if release.retired_at else None
    )
    return payload


# ---------------------------------------------------------------------------
# Image records
# ---------------------------------------------------------------------------


@router.get("/images", summary="Browse records, including retired ones")
async def list_images(request: Request, db: SessionDep, claims: CanRead) -> CanonicalJSONResponse:
    # Same query contract as the public endpoint, so a filter a curator built
    # in Explore can be pasted straight into the dashboard.
    query = parse_image_query(dict(request.query_params))
    # The dashboard sees drafts: a curator has to review the release they
    # are assembling before publishing it.
    page = await repository.list_images(db, query, decode_cursor(query.cursor), include_drafts=True)
    return envelope(
        request,
        [service.render_record(row) for row in page.rows],
        meta={
            "count": len(page.rows),
            "total": page.total,
            "limit": query.limit,
            "nextCursor": page.next_cursor,
        },
    )


@router.get("/images/{record_id}", summary="One record, with editable metadata")
async def get_image(
    record_id: str, request: Request, db: SessionDep, claims: CanRead
) -> CanonicalJSONResponse:
    record = await admin_service.get_record_or_404(db, record_id)
    release = await db.get(Release, record.release_id)
    row = await repository.get_image(db, record_id, include_drafts=True)

    return envelope(
        request,
        {
            **(service.render_record(row) if row else {"id": record.id}),
            "editable": release is not None and release.status is ReleaseStatus.DRAFT,
            "retiredAt": record.retired_at.isoformat().replace("+00:00", "Z")
            if record.retired_at
            else None,
            "conditionTags": list(record.condition_tags or []),
            "release": _release_json(release) if release else None,
        },
    )


@router.patch("/images/{record_id}", summary="Edit a record's provenance")
async def update_image(
    record_id: str,
    payload: ImageRecordWrite,
    request: Request,
    db: SessionDep,
    claims: CanWrite,
) -> CanonicalJSONResponse:
    record = await admin_service.get_record_or_404(db, record_id)
    before = admin_service.snapshot_record(record)

    await admin_service.update_record(db, record, payload)
    after = admin_service.snapshot_record(record)

    await audit.record(
        db,
        action="image_record.update",
        entity_type="image_record",
        entity_id=record.id,
        before=before,
        after=after,
        summary=", ".join(audit.changed_fields(before, after)) or "no change",
        **_actor(request, claims),
    )
    await db.commit()

    row = await repository.get_image(db, record_id, include_drafts=True)
    return envelope(request, service.render_record(row) if row else {"id": record.id})


@router.post("/images/bulk", summary="Apply provenance to many records")
async def bulk_update_images(
    payload: BulkImageEdit, request: Request, db: SessionDep, claims: CanWrite
) -> CanonicalJSONResponse:
    updated, skipped = await admin_service.bulk_update_records(db, payload)

    for record in updated:
        await audit.record(
            db,
            action="image_record.bulk_update",
            entity_type="image_record",
            entity_id=record.id,
            after=admin_service.snapshot_record(record),
            summary=f"bulk edit of {len(updated)} records",
            **_actor(request, claims),
        )
    await db.commit()

    return envelope(
        request,
        {
            "updated": [record.id for record in updated],
            # Named rather than swallowed: a frame in a published release is
            # skipped on purpose, and the curator needs to know which.
            "skipped": skipped,
        },
        meta={"count": len(updated), "skipped": len(skipped)},
    )


@router.post("/images/{record_id}/retire", summary="Hide a record from the public API")
async def retire_image(
    record_id: str, request: Request, db: SessionDep, claims: CanWrite
) -> CanonicalJSONResponse:
    record = await admin_service.get_record_or_404(db, record_id)
    await admin_service.retire_record(db, record)
    await audit.record(
        db,
        action="image_record.retire",
        entity_type="image_record",
        entity_id=record.id,
        summary="retired",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, {"id": record.id, "retired": True})


@router.post("/images/{record_id}/restore", summary="Return a record to the archive")
async def restore_image(
    record_id: str, request: Request, db: SessionDep, claims: CanWrite
) -> CanonicalJSONResponse:
    record = await admin_service.get_record_or_404(db, record_id)
    await admin_service.restore_record(db, record)
    await audit.record(
        db,
        action="image_record.restore",
        entity_type="image_record",
        entity_id=record.id,
        summary="restored",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, {"id": record.id, "retired": False})


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

#: Read in chunks with a running total, so an oversized upload is refused
#: after a megabyte rather than after the whole thing has been buffered.
_UPLOAD_CHUNK = 1024 * 1024


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_UPLOAD_CHUNK):
        total += len(chunk)
        if total > limit:
            raise PayloadTooLargeError(
                f"That file is larger than the {limit / 1024**2:.0f} MB limit."
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.put(
    "/images/{record_id}/artifacts/{artifact_type}",
    summary="Attach or replace a frame or mask",
)
async def upload_artifact(
    record_id: str,
    artifact_type: str,
    request: Request,
    db: SessionDep,
    store: StoreDep,
    claims: CanWrite,
    file: Annotated[UploadFile, File()],
) -> CanonicalJSONResponse:
    if artifact_type not in ("source", "mask"):
        raise UnprocessableUploadError(
            "An artifact is either a 'source' frame or a 'mask'. Thumbnails and "
            "WebP renditions are generated, not uploaded."
        )

    record = await admin_service.get_record_or_404(db, record_id)
    data = await _read_upload(file, get_settings().max_upload_bytes)

    result = await uploads.replace_artifact(
        db,
        store,
        record=record,
        artifact_type=cast("uploads.PrimaryType", artifact_type),
        data=data,
    )

    await audit.record(
        db,
        action=f"artifact.{'replace' if result.replaced else 'attach'}",
        entity_type="image_record",
        entity_id=record.id,
        after={
            "type": result.artifact_type,
            "checksum": result.checksum,
            "bytes": result.bytes,
            "dimensions": f"{result.width}x{result.height}",
        },
        summary=(
            f"{'replaced' if result.replaced else 'attached'} the "
            f"{result.artifact_type} ({file.filename})"
        ),
        **_actor(request, claims),
    )
    await db.commit()

    return envelope(
        request,
        {
            "id": result.record_id,
            "type": result.artifact_type,
            "objectKey": result.object_key,
            "checksum": result.checksum,
            "bytes": result.bytes,
            "width": result.width,
            "height": result.height,
            "replaced": result.replaced,
            "derivatives": len(result.derivatives),
            # Replacing a mask invalidates the measurement taken from the old
            # one, so the dashboard needs to say so rather than leaving a
            # stale number on screen.
            "measurementCleared": result.artifact_type == "mask" and result.replaced,
        },
    )


@router.delete(
    "/images/{record_id}/artifacts/{artifact_type}",
    summary="Remove a frame or mask",
)
async def remove_artifact(
    record_id: str,
    artifact_type: str,
    request: Request,
    db: SessionDep,
    store: StoreDep,
    claims: CanWrite,
) -> CanonicalJSONResponse:
    if artifact_type not in ("source", "mask"):
        raise UnprocessableUploadError("An artifact is either a 'source' or a 'mask'.")

    record = await admin_service.get_record_or_404(db, record_id)
    await uploads.delete_artifact(
        db,
        store,
        record=record,
        artifact_type=cast("uploads.PrimaryType", artifact_type),
    )

    await audit.record(
        db,
        action="artifact.remove",
        entity_type="image_record",
        entity_id=record.id,
        before={"type": artifact_type},
        summary=f"removed the {artifact_type}",
        **_actor(request, claims),
    )
    await db.commit()

    return envelope(request, {"id": record.id, "removed": artifact_type})


# ---------------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------------


@router.get("/collections", summary="Collections with their release history")
async def list_collections(
    request: Request, db: SessionDep, claims: CanRead
) -> CanonicalJSONResponse:
    collections = await repository.list_collections(db)
    payload = []
    for collection in collections:
        stats = await repository.collection_stats(db, collection.id)
        payload.append(
            {
                "id": str(collection.id),
                "slug": collection.slug,
                "title": collection.title,
                "description": collection.description,
                "locationName": collection.location_name,
                "latitude": float(collection.latitude) if collection.latitude else None,
                "longitude": float(collection.longitude) if collection.longitude else None,
                "instrument": collection.instrument,
                "license": collection.license,
                "citation": collection.citation,
                "doi": collection.doi,
                "methodsUrl": collection.methods_url,
                "publicationUrl": collection.publication_url,
                "position": collection.position,
                "images": stats.images,
                "segmented": stats.segmented,
                "sequenceIds": stats.sequence_ids,
                # The same aggregates the public payload carries, so the
                # dashboard can show what the pipeline measured next to the
                # fields a curator supplies.
                "meanCloudCoverOktas": (
                    None if stats.mean_oktas is None else round(stats.mean_oktas, 6)
                ),
                "maskRegistration": [
                    {"sequenceId": sequence_id, "scale": scale, "corrected": scale > 1}
                    for sequence_id, scale in stats.mask_registration
                ],
                "releases": [_release_json(release) for release in collection.releases],
            }
        )
    return envelope(request, payload, meta={"count": len(payload)})


@router.post("/collections", summary="Create a collection")
async def create_collection(
    payload: CollectionCreate, request: Request, db: SessionDep, claims: CanWrite
) -> CanonicalJSONResponse:
    collection = await admin_service.create_collection(db, payload)
    await audit.record(
        db,
        action="collection.create",
        entity_type="collection",
        entity_id=collection.slug,
        after=admin_service.snapshot_collection(collection),
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, {"id": str(collection.id), "slug": collection.slug})


@router.patch("/collections/{slug}", summary="Edit a collection's metadata")
async def update_collection(
    slug: str,
    payload: CollectionWrite,
    request: Request,
    db: SessionDep,
    claims: CanWrite,
) -> CanonicalJSONResponse:
    collection = await admin_service.get_collection_or_404(db, slug)
    before = admin_service.snapshot_collection(collection)

    await admin_service.update_collection(db, collection, payload)
    after = admin_service.snapshot_collection(collection)

    await audit.record(
        db,
        action="collection.update",
        entity_type="collection",
        entity_id=collection.slug,
        before=before,
        after=after,
        summary=", ".join(audit.changed_fields(before, after)) or "no change",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, await service.render_collection(db, collection))


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


@router.post("/collections/{slug}/releases", summary="Start a draft release")
async def create_release(
    slug: str, payload: ReleaseCreate, request: Request, db: SessionDep, claims: CanWrite
) -> CanonicalJSONResponse:
    collection = await admin_service.get_collection_or_404(db, slug)
    release = await admin_service.create_release(db, collection, payload)
    await audit.record(
        db,
        action="release.create",
        entity_type="release",
        entity_id=str(release.id),
        after={"collection": slug, "version": release.version, "status": "DRAFT"},
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, _release_json(release))


@router.post("/releases/{release_id}/status", summary="Publish or retire a release")
async def transition_release(
    release_id: uuid.UUID,
    payload: ReleaseTransition,
    request: Request,
    db: SessionDep,
    claims: CanPublish,
) -> CanonicalJSONResponse:
    release = await admin_service.get_release_or_404(db, release_id)
    before = {"status": release.status.value, "current": release.current}

    if payload.status == "PUBLISHED":
        await admin_service.publish_release(db, release, make_current=payload.current)
    else:
        await admin_service.retire_release(db, release)

    after = {"status": release.status.value, "current": release.current}
    await audit.record(
        db,
        action=f"release.{payload.status.lower()}",
        entity_type="release",
        entity_id=str(release.id),
        before=before,
        after=after,
        summary=f"{before['status']} -> {after['status']}",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, _release_json(release))


@router.delete("/releases/{release_id}", summary="Delete a draft release")
async def delete_release(
    release_id: uuid.UUID, request: Request, db: SessionDep, claims: CanWrite
) -> CanonicalJSONResponse:
    release = await admin_service.get_release_or_404(db, release_id)
    if release.status is not ReleaseStatus.DRAFT:
        # Deleting a published release would take published records with it.
        # Retiring is the supported way to withdraw one.
        raise ConflictError("Only a draft can be deleted. Retire a published release instead.")

    await audit.record(
        db,
        action="release.delete",
        entity_type="release",
        entity_id=str(release.id),
        before={"version": release.version, "status": release.status.value},
        **_actor(request, claims),
    )
    await db.delete(release)
    await db.commit()
    return envelope(request, {"deleted": str(release_id)})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@router.get("/audit", summary="Who changed what")
async def list_audit(
    request: Request,
    db: SessionDep,
    claims: CanAudit,
    entity_type: Annotated[str | None, Query(alias="entityType")] = None,
    entity_id: Annotated[str | None, Query(alias="entityId")] = None,
    action: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CanonicalJSONResponse:
    stmt = audit.query(entity_type=entity_type, entity_id=entity_id, action=action)
    events = (await db.execute(stmt.limit(limit))).scalars().all()
    return envelope(
        request,
        [audit.to_json(event) for event in events],
        meta={"count": len(events)},
    )


__all__ = ["router"]
