import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID, uuid4

import uvicorn
from botocore.exceptions import BotoCoreError
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend import chat, embeddings
from backend.database import get_session
from backend.models import IngestionJob, Organisation
from backend.queue import QUEUE_CONFIGURED, broker
from backend.schemas import (
    ChatRequest,
    ChatResponse,
    ChatUsageView,
    CitationView,
    ConversationMessageView,
    ConversationView,
    DocumentCreate,
    DocumentView,
    JobView,
    OrganisationCreate,
    OrganisationView,
    SearchRequest,
    SearchResultView,
    SearchView,
    SourceCreate,
    SourceView,
    TestJobCreate,
    UploadView,
)
from backend.services import conversations, ingestion, knowledge, rag
from backend.services import documents as document_service
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
        "stage": "grounded-chat",
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


@app.post("/api/organisations/{organisation_id}/search", response_model=SearchView)
def search_documents(
    organisation_id: UUID,
    payload: SearchRequest,
    session: Annotated[Session, Depends(get_session)],
) -> SearchView:
    if session.get(Organisation, organisation_id) is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    try:
        provider = embeddings.get_embedding_provider()
        query_embedding = provider.embed_query(payload.query)
    except embeddings.InvalidEmbeddingInput as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (RuntimeError, OSError) as error:
        raise HTTPException(
            status_code=503, detail="Embedding provider is unavailable"
        ) from error

    hits = knowledge.search_chunks(
        session,
        organisation_id,
        query_embedding,
        provider.model_id,
        payload.limit,
        payload.source_id,
        payload.document_id,
    )
    return SearchView(
        embedding_model=provider.model_id,
        results=[
            SearchResultView(
                chunk_id=hit.chunk.id,
                document_id=hit.document.id,
                document_title=hit.document.title,
                source_id=hit.document.source_id,
                source_name=hit.source.name if hit.source else None,
                content=hit.chunk.content,
                page=hit.chunk.page,
                section=hit.chunk.section,
                score=hit.score,
            )
            for hit in hits
        ],
    )


def citation_view(hit: knowledge.SearchHit) -> CitationView:
    return CitationView(
        chunk_id=hit.chunk.id,
        document_id=hit.document.id,
        document_title=hit.document.title,
        source_id=hit.document.source_id,
        source_name=hit.source.name if hit.source else None,
        page=hit.chunk.page,
        section=hit.chunk.section,
        snippet=hit.chunk.content,
    )


@app.post("/api/organisations/{organisation_id}/chat", response_model=ChatResponse)
def chat_with_knowledge(
    organisation_id: UUID,
    payload: ChatRequest,
    session: Annotated[Session, Depends(get_session)],
) -> ChatResponse:
    if session.get(Organisation, organisation_id) is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    conversation = None
    conversation_id = payload.conversation_id or uuid4()
    if payload.conversation_id is not None:
        conversation = conversations.get_conversation(
            session, organisation_id, payload.conversation_id
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        history = conversations.get_history(
            session,
            organisation_id,
            conversation_id,
            rag.MAX_HISTORY_MESSAGES,
        )
    else:
        history = []

    try:
        embedding_provider = embeddings.get_embedding_provider()
        query_embedding = embedding_provider.embed_query(payload.message)
    except embeddings.InvalidEmbeddingInput as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (RuntimeError, OSError) as error:
        raise HTTPException(
            status_code=503, detail="Embedding provider is unavailable"
        ) from error

    hits = knowledge.search_chunks(
        session,
        organisation_id,
        query_embedding,
        embedding_provider.model_id,
        payload.top_k,
    )
    session.commit()
    has_evidence = any(hit.score >= rag.MIN_RETRIEVAL_SCORE for hit in hits)
    try:
        chat_provider = chat.get_chat_provider() if has_evidence else None
        grounded = rag.answer_question(payload.message, history, hits, chat_provider)
    except chat.ChatProviderError as error:
        raise HTTPException(
            status_code=503, detail="Chat provider is unavailable"
        ) from error

    citations = [citation_view(hit) for hit in grounded.citations]
    completion = grounded.completion
    conversations.save_turn(
        session,
        organisation_id,
        conversation,
        conversation_id,
        payload.message,
        grounded.content,
        [citation.model_dump(mode="json") for citation in citations],
        completion.model_id if completion else None,
        completion.input_tokens if completion else None,
        completion.output_tokens if completion else None,
    )
    return ChatResponse(
        conversation_id=conversation_id,
        answer=grounded.content,
        citations=citations,
        model_id=completion.model_id if completion else None,
        usage=ChatUsageView(
            input_tokens=completion.input_tokens if completion else None,
            output_tokens=completion.output_tokens if completion else None,
        ),
    )


@app.get(
    "/api/organisations/{organisation_id}/conversations/{conversation_id}",
    response_model=ConversationView,
)
def get_conversation(
    organisation_id: UUID,
    conversation_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ConversationView:
    conversation = conversations.get_conversation(
        session, organisation_id, conversation_id
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationView(
        id=conversation.id,
        organisation_id=conversation.organisation_id,
        title=conversation.title,
        created_at=conversation.created_at,
        messages=[
            ConversationMessageView.model_validate(message)
            for message in conversations.list_messages(
                session, organisation_id, conversation_id, limit
            )
        ],
    )


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
