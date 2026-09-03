"""Recording who changed what.

This is a scientific archive, so the provenance of an edit matters as much as
the provenance of a measurement: a number that changed should be traceable to
the person who changed it and the value it held before.

Every admin mutation records one event. The actor's email is snapshotted
alongside the foreign key so the trail stays readable after an account is
removed, and both the before and after state are kept so a change can be read
without reconstructing it from adjacent events.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.domains.audit.models import AuditEvent

#: Values that must never be written to the log, however they arrive.
REDACTED = "[redacted]"
SENSITIVE_FIELDS = frozenset(
    {"password", "password_hash", "token", "token_hash", "secret", "secret_key"}
)


def scrub(state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip anything that should not be readable in a log."""
    if state is None:
        return None
    return {
        key: (REDACTED if key.lower() in SENSITIVE_FIELDS else value)
        for key, value in state.items()
    }


def changed_fields(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    if before is None or after is None:
        return []
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


async def record(
    session: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor_id: uuid.UUID | None = None,
    actor_email: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    summary: str | None = None,
    request_id: str | None = None,
    ip: str | None = None,
) -> AuditEvent:
    """Append one event.

    Deliberately part of the caller's transaction: if the change rolls back,
    so does the claim that it happened.
    """
    event = AuditEvent(
        actor_user_id=actor_id,
        actor_email=actor_email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=scrub(before),
        after=scrub(after),
        summary=summary,
        request_id=request_id,
        ip=ip,
    )
    session.add(event)
    return event


def query(
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
) -> Select[Any]:
    stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    if entity_type:
        stmt = stmt.where(AuditEvent.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditEvent.entity_id == entity_id)
    if actor_id:
        stmt = stmt.where(AuditEvent.actor_user_id == actor_id)
    if action:
        stmt = stmt.where(AuditEvent.action.startswith(action))
    return stmt


def to_json(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "action": event.action,
        "entityType": event.entity_type,
        "entityId": event.entity_id,
        "actor": {
            "id": str(event.actor_user_id) if event.actor_user_id else None,
            "email": event.actor_email,
        },
        "before": event.before,
        "after": event.after,
        "changed": changed_fields(event.before, event.after),
        "summary": event.summary,
        "createdAt": event.created_at.isoformat().replace("+00:00", "Z"),
    }
