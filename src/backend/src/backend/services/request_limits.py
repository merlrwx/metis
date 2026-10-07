"""Small shared request budgets and streaming body bounds; no prompt storage."""

import os
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select
from starlette.responses import JSONResponse

from backend import database
from backend.models import AuditEvent, Organisation
from backend.services import audit
from backend.services.ingestion import MAX_UPLOAD_BYTES

JSON_BODY_LIMIT = 64 * 1024
UPLOAD_BODY_LIMIT = MAX_UPLOAD_BYTES + JSON_BODY_LIMIT


def configured_limits():
    user_limit = int(os.environ.get("METIS_USER_REQUESTS_PER_MINUTE", "30"))
    organisation_limit = int(os.environ.get("METIS_ORG_REQUESTS_PER_MINUTE", "120"))
    if not all(1 <= value <= 10000 for value in (user_limit, organisation_limit)):
        raise ValueError("Request budgets must be between 1 and 10,000 per minute")
    return user_limit, organisation_limit


def enforce(organisation_id, user_id, operation):
    user_limit, organisation_limit = configured_limits()
    now = datetime.now(UTC)
    window = now.replace(second=0, microsecond=0)
    with database.SessionLocal() as session:
        # Serialise admission across API processes using the existing org row.
        organisation = session.scalar(
            select(Organisation.id)
            .where(Organisation.id == organisation_id)
            .with_for_update()
        )
        if organisation is None:
            raise HTTPException(status_code=404, detail="Organisation not found")
        statement = select(func.count(AuditEvent.id)).where(
            AuditEvent.organisation_id == organisation_id,
            AuditEvent.action == "request.accepted",
            AuditEvent.created_at >= window,
        )
        total = session.scalar(statement)
        personal = session.scalar(statement.where(AuditEvent.actor_user_id == user_id))
        if total >= organisation_limit or personal >= user_limit:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Wait a minute and retry.",
                headers={"Retry-After": str(60 - now.second)},
            )
        audit.record_event(
            session,
            organisation_id,
            user_id,
            "request.accepted",
            "organisation",
            organisation_id,
            {"operation": operation},
        )
        # Failed model requests still consume admission; UUID-cached replies do not.
        session.commit()


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(status_code=413, detail="Request body exceeds its size limit")


class RequestSizeLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        limit = (
            UPLOAD_BODY_LIMIT
            if scope["path"].endswith("/documents/upload")
            else JSON_BODY_LIMIT
        )
        declared = headers.get(b"content-length")
        try:
            if declared is not None and int(declared) > limit:
                raise BodyTooLarge()
        except (ValueError, BodyTooLarge):
            return await JSONResponse(
                {"detail": "Request body exceeds its size limit"}, status_code=413
            )(scope, receive, send)
        consumed = 0

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise BodyTooLarge()
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except BodyTooLarge:
            await JSONResponse(
                {"detail": "Request body exceeds its size limit"}, status_code=413
            )(scope, receive, send)
