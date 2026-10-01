import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import uvicorn
from botocore.exceptions import BotoCoreError
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_session
from backend.models import IngestionJob
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
    UploadView,
)
from backend.services import documents as document_service
from backend.services import ingestion, knowledge
from backend.services import jobs as job_service
from backend.storage import get_object_storage
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


async def publish_job(session: Session, job: IngestionJob) -> None:
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


def document_view(session: Session, document) -> DocumentView:
    job = document_service.get_document_job(
        session, document.organisation_id, document.current_version_id
    )
    status = job.status if job is not None else None
    if status == "completed":
        status = "indexed"
    return DocumentView(
        id=document.id,
        organisation_id=document.organisation_id,
        source_id=document.source_id,
        title=document.title,
        source_uri=document.source_uri,
        current_version_id=document.current_version_id,
        created_at=document.created_at,
        updated_at=document.updated_at,
        ingestion_status=status,
        ingestion_error=job.error if job is not None else None,
    )


@app.post(
    "/api/organisations/{organisation_id}/documents/upload",
    response_model=UploadView,
    status_code=202,
)
async def upload_document(
    organisation_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    source_id: Annotated[UUID | None, Form()] = None,
) -> UploadView:
    if not QUEUE_CONFIGURED:
        raise HTTPException(status_code=503, detail="Background queue is unavailable")
    try:
        filename = ingestion.clean_filename(file.filename or "")
    except ingestion.UnsupportedDocument as error:
        raise HTTPException(status_code=415, detail=str(error)) from error
    data = await file.read(ingestion.MAX_UPLOAD_BYTES + 1)
    try:
        mime_type = ingestion.validate_upload(filename, file.content_type or "", data)
    except ingestion.DocumentTooLarge as error:
        raise HTTPException(status_code=413, detail=str(error)) from error
    except ingestion.UnsupportedDocument as error:
        raise HTTPException(status_code=415, detail=str(error)) from error

    try:
        document, job, created = document_service.upload_document(
            session,
            organisation_id,
            filename,
            mime_type,
            data,
            get_object_storage(),
            source_id,
        )
    except (BotoCoreError, OSError, RuntimeError) as error:
        raise HTTPException(
            status_code=503, detail="Document storage is unavailable"
        ) from error
    if document is None or job is None:
        raise HTTPException(status_code=404, detail="Organisation or source not found")

    if created or job.status == "pending":
        await publish_job(session, job)
    session.expire_all()
    job = job_service.get_job(session, organisation_id, job.id)
    document = knowledge.get_document(session, organisation_id, document.id)
    return UploadView(document=document_view(session, document), job=job)


@app.get(
    "/api/organisations/{organisation_id}/documents/{document_id}",
    response_model=DocumentView,
)
def get_document(
    organisation_id: UUID,
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
) -> DocumentView:
    document = knowledge.get_document(session, organisation_id, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return document_view(session, document)


@app.get(
    "/api/organisations/{organisation_id}/documents",
    response_model=list[DocumentView],
)
def list_documents(
    organisation_id: UUID, session: Annotated[Session, Depends(get_session)]
) -> list[DocumentView]:
    return [
        document_view(session, document)
        for document in knowledge.list_documents(session, organisation_id)
    ]


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
        await publish_job(session, job)
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
