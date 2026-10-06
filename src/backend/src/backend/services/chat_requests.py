"""Transaction-scoped deduplication of explicitly identified chat requests."""

import hashlib
import json
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import ChatRequestRecord


def begin(
    session: Session,
    organisation_id: UUID,
    user_id: UUID,
    request_id: UUID | None,
    payload: dict,
):
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()
    ).hexdigest()
    if request_id is None:
        return fingerprint, None
    if session.bind.dialect.name == "postgresql":
        key = f"metis-chat:{organisation_id}:{user_id}:{request_id}"
        session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )
    record = session.get(ChatRequestRecord, (organisation_id, user_id, request_id))
    if record is not None and record.fingerprint != fingerprint:
        raise ValueError("Request ID was already used for another question or scope")
    return fingerprint, record


def finish(
    session: Session,
    organisation_id: UUID,
    user_id: UUID,
    request_id: UUID | None,
    fingerprint: str,
    response: dict,
):
    if request_id is not None:
        session.add(
            ChatRequestRecord(
                organisation_id=organisation_id,
                user_id=user_id,
                request_id=request_id,
                conversation_id=UUID(response["conversation_id"]),
                fingerprint=fingerprint,
                response=response,
            )
        )
