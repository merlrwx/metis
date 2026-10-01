import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import AuditEvent


def record_event(
    session: Session,
    organisation_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        organisation_id=organisation_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details or {},
    )
    session.add(event)
    return event


def list_events(session: Session, organisation_id: uuid.UUID, limit: int):
    return list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.organisation_id == organisation_id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
            .limit(limit)
        )
    )
