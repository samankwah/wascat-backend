"""Dashboard endpoints for the controlled vocabularies."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from wascat.api.deps import SessionDep, require_permission
from wascat.core.envelope import CanonicalJSONResponse, envelope
from wascat.core.errors import NotFoundError
from wascat.core.security.tokens import AccessClaims
from wascat.domains.audit import service as audit
from wascat.domains.catalog.admin_schemas import (
    VocabularyMerge,
    VocabularyReorder,
    VocabularyTermWrite,
)
from wascat.domains.iam.models import VOCAB_READ, VOCAB_WRITE
from wascat.domains.vocab import service
from wascat.domains.vocab.models import CONTRACT_KINDS, VocabKind, VocabularyTerm

router = APIRouter(prefix="/vocabulary", tags=["admin:vocabulary"])

CanRead = Annotated[AccessClaims, Depends(require_permission(VOCAB_READ))]
CanWrite = Annotated[AccessClaims, Depends(require_permission(VOCAB_WRITE))]


def _actor(request: Request, claims: AccessClaims) -> dict[str, Any]:
    return {
        "actor_id": claims.user_id,
        "actor_email": claims.email,
        "request_id": getattr(request.state, "request_id", None),
        "ip": request.client.host if request.client else None,
    }


def _parse_kind(value: str) -> VocabKind:
    try:
        return VocabKind(value)
    except ValueError as exc:
        known = ", ".join(kind.value for kind in VocabKind)
        raise NotFoundError(f"No such vocabulary. Available: {known}") from exc


def _term_json(term: VocabularyTerm, records: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(term.id),
        "kind": term.kind.value,
        "slug": term.slug,
        "label": term.label,
        "description": term.description,
        "position": term.position,
        # A term the public API validates against cannot be removed or merged
        # away, and the UI needs to say why rather than offering a button that
        # always fails.
        "system": term.system,
        "retiredAt": term.retired_at.isoformat().replace("+00:00", "Z")
        if term.retired_at
        else None,
        "mergedIntoId": str(term.merged_into_id) if term.merged_into_id else None,
        "aliases": sorted(alias.value for alias in term.aliases),
    }
    if records is not None:
        payload["records"] = records
    return payload


@router.get("", summary="Every vocabulary and its terms")
async def list_vocabularies(
    request: Request, db: SessionDep, claims: CanRead
) -> CanonicalJSONResponse:
    payload = []
    for kind in VocabKind:
        usages = await service.list_terms(db, kind)
        payload.append(
            {
                "kind": kind.value,
                "label": kind.value.replace("_", " ").capitalize(),
                # Whether adding a term here changes what the public API
                # accepts. The dashboard explains the difference rather than
                # letting someone discover it by being refused.
                "frozen": kind in CONTRACT_KINDS,
                "terms": [_term_json(usage.term, usage.records) for usage in usages],
            }
        )
    return envelope(request, payload, meta={"count": len(payload)})


@router.get("/{kind}", summary="One vocabulary")
async def list_terms(
    kind: str, request: Request, db: SessionDep, claims: CanRead
) -> CanonicalJSONResponse:
    parsed = _parse_kind(kind)
    usages = await service.list_terms(db, parsed)
    return envelope(
        request,
        [_term_json(usage.term, usage.records) for usage in usages],
        meta={"kind": parsed.value, "frozen": parsed in CONTRACT_KINDS, "count": len(usages)},
    )


@router.post("/{kind}", summary="Add a term")
async def create_term(
    kind: str,
    payload: VocabularyTermWrite,
    request: Request,
    db: SessionDep,
    claims: CanWrite,
) -> CanonicalJSONResponse:
    parsed = _parse_kind(kind)
    term = await service.create_term(
        db, kind=parsed, label=payload.label, description=payload.description
    )
    await audit.record(
        db,
        action="vocab.create",
        entity_type="vocabulary_term",
        entity_id=str(term.id),
        after={"kind": parsed.value, "label": term.label, "slug": term.slug},
        summary=f"added {term.label!r} to {parsed.value}",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, _term_json(term, 0))


@router.patch("/terms/{term_id}", summary="Rename a term")
async def rename_term(
    term_id: uuid.UUID,
    payload: VocabularyTermWrite,
    request: Request,
    db: SessionDep,
    claims: CanWrite,
) -> CanonicalJSONResponse:
    term = await service.get_term(db, term_id)
    before = {"label": term.label, "description": term.description}

    await service.rename_term(db, term, label=payload.label, description=payload.description)
    moved = await service.count_usage(db, term)

    await audit.record(
        db,
        action="vocab.rename",
        entity_type="vocabulary_term",
        entity_id=str(term.id),
        before=before,
        after={"label": term.label, "description": term.description},
        summary=(
            f"renamed {before['label']!r} to {term.label!r}; "
            f"{moved:,} record(s) updated, old value kept as an alias"
        ),
        **_actor(request, claims),
    )
    await db.commit()
    await db.refresh(term, ["aliases"])
    return envelope(request, _term_json(term, moved))


@router.post("/terms/{term_id}/merge", summary="Merge one term into another")
async def merge_term(
    term_id: uuid.UUID,
    payload: VocabularyMerge,
    request: Request,
    db: SessionDep,
    claims: CanWrite,
) -> CanonicalJSONResponse:
    loser = await service.get_term(db, term_id)
    winner = await service.get_term(db, uuid.UUID(payload.into_id))

    moved = await service.merge_terms(db, loser=loser, winner=winner)

    await audit.record(
        db,
        action="vocab.merge",
        entity_type="vocabulary_term",
        entity_id=str(loser.id),
        before={"label": loser.label, "records": moved},
        after={"mergedInto": winner.label},
        summary=f"merged {loser.label!r} into {winner.label!r}; {moved:,} record(s) moved",
        **_actor(request, claims),
    )
    await db.commit()
    await db.refresh(winner, ["aliases"])

    return envelope(
        request,
        {
            "merged": _term_json(loser),
            "into": _term_json(winner, await service.count_usage(db, winner)),
            "recordsMoved": moved,
        },
    )


@router.post("/terms/{term_id}/retire", summary="Take a term out of use")
async def retire_term(
    term_id: uuid.UUID, request: Request, db: SessionDep, claims: CanWrite
) -> CanonicalJSONResponse:
    term = await service.get_term(db, term_id)
    await service.retire_term(db, term)
    await audit.record(
        db,
        action="vocab.retire",
        entity_type="vocabulary_term",
        entity_id=str(term.id),
        before={"label": term.label},
        summary=f"retired {term.label!r}",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, _term_json(term, 0))


@router.post("/terms/{term_id}/restore", summary="Return a term to use")
async def restore_term(
    term_id: uuid.UUID, request: Request, db: SessionDep, claims: CanWrite
) -> CanonicalJSONResponse:
    term = await service.get_term(db, term_id)
    await service.restore_term(db, term)
    await audit.record(
        db,
        action="vocab.restore",
        entity_type="vocabulary_term",
        entity_id=str(term.id),
        summary=f"restored {term.label!r}",
        **_actor(request, claims),
    )
    await db.commit()
    return envelope(request, _term_json(term, await service.count_usage(db, term)))


@router.post("/{kind}/order", summary="Reorder a vocabulary")
async def reorder_terms(
    kind: str,
    payload: VocabularyReorder,
    request: Request,
    db: SessionDep,
    claims: CanWrite,
) -> CanonicalJSONResponse:
    parsed = _parse_kind(kind)
    await service.reorder(db, parsed, [uuid.UUID(value) for value in payload.ids])
    await audit.record(
        db,
        action="vocab.reorder",
        entity_type="vocabulary",
        entity_id=parsed.value,
        summary=f"reordered {len(payload.ids)} terms",
        **_actor(request, claims),
    )
    await db.commit()

    usages = await service.list_terms(db, parsed)
    return envelope(request, [_term_json(usage.term, usage.records) for usage in usages])


__all__ = ["router"]
