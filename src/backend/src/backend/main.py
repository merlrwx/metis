import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_session
from backend.queue import QUEUE_CONFIGURED, broker
from backend.schemas import (
    DocumentCreate,
    DocumentView,
    JobView,
    OrganisationCreate,
    OrganisationView,
    SourceCreate,
    SourceView,
    TestJobCreate,
)
from backend.services import jobs as job_service
from backend.services import knowledge
from backend.tasks import process_ingestion_job


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if QUEUE_CONFIGURED:
        await broker.startup()
    try:
        yield
    finally:
        if QUEUE_CONFIGURED:
            await broker.shutdown()


app = FastAPI(title="Metis API", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health(session: Annotated[Session, Depends(get_session)]) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/info")
def info() -> dict[str, str]:
    return {
        "name": "Metis",
        "description": "Grounded answers from your organisation's knowledge.",
        "stage": "async-processing",
    }


@app.post("/api/organisations", response_model=OrganisationView, status_code=201)
def create_organisation(
    payload: OrganisationCreate, session: Annotated[Session, Depends(get_session)]
) -> OrganisationView:
    return knowledge.create_organisation(session, payload.name)


@app.post(
    "/api/organisations/{organisation_id}/sources",
    response_model=SourceView,
    status_code=201,
)
def create_source(
    organisation_id: UUID,
    payload: SourceCreate,
    session: Annotated[Session, Depends(get_session)],
) -> SourceView:
    source = knowledge.create_source(
        session,
        organisation_id,
        payload.name,
        payload.type,
        payload.configuration,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    return source


@app.post(
    "/api/organisations/{organisation_id}/documents",
    response_model=DocumentView,
    status_code=201,
)
def create_document(
    organisation_id: UUID,
    payload: DocumentCreate,
    session: Annotated[Session, Depends(get_session)],
) -> DocumentView:
    document = knowledge.create_document(
        session,
        organisation_id,
        payload.title,
        payload.source_id,
        payload.source_uri,
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Organisation or source not found")
    return document


@app.get(
    "/api/organisations/{organisation_id}/documents",
    response_model=list[DocumentView],
)
def list_documents(
    organisation_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> list[DocumentView]:
    return knowledge.list_documents(session, organisation_id)


@app.post("/api/jobs/test", response_model=JobView, status_code=202)
async def create_test_job(
    payload: TestJobCreate,
    session: Annotated[Session, Depends(get_session)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=255)
    ] = None,
) -> JobView:
    if not QUEUE_CONFIGURED:
        raise HTTPException(status_code=503, detail="Background queue is unavailable")
    job, created = job_service.create_test_job(
        session, payload.organisation_id, idempotency_key
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    if created or job.status == "pending":
        try:
            await process_ingestion_job.kiq(str(job.id), str(job.organisation_id))
        except Exception as error:
            job_service.record_enqueue_failure(
                session,
                job.organisation_id,
                job.id,
                f"Unable to publish job: {type(error).__name__}",
            )
            raise HTTPException(
                status_code=503, detail="Background queue is unavailable"
            ) from error
        job_service.mark_queued(session, job.organisation_id, job.id)
    session.expire_all()
    return job_service.get_job(session, payload.organisation_id, job.id) or job


@app.get("/api/organisations/{organisation_id}/jobs/{job_id}", response_model=JobView)
def get_ingestion_job(
    organisation_id: UUID,
    job_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> JobView:
    job = job_service.get_job(session, organisation_id, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def main() -> None:
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
    )
