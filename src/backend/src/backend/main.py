import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

import uvicorn
from botocore.exceptions import BotoCoreError, ClientError
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
from fastapi.responses import JSONResponse, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import auth, chat, embeddings, observability
from backend.database import get_session
from backend.models import (
    IngestionJob,
    Organisation,
    OrganisationMembership,
    User,
)
from backend.queue import QUEUE_CONFIGURED, broker
from backend.schemas import (
    AuditEventView,
    ChatRequest,
    ChatResponse,
    ChatUsageView,
    CitationView,
    ConversationMessageView,
    ConversationView,
    CurrentUserView,
    DocumentCreate,
    DocumentView,
    JobView,
    KnowledgeGroupView,
    KnowledgeGroupWrite,
    MemberAdd,
    MembershipView,
    OrganisationCreate,
    OrganisationMemberView,
    OrganisationView,
    SearchRequest,
    SearchResultView,
    SearchView,
    SourceCreate,
    SourceView,
    TestJobCreate,
    TokenView,
    UploadView,
    UserRegister,
    UserView,
)
from backend.services import audit, conversations, groups, ingestion, knowledge, rag
from backend.services import documents as document_service
from backend.services import jobs as job_service
from backend.storage import get_object_storage
from backend.tasks import process_ingestion_job, synchronize_source


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    if os.environ.get("METIS_EMBEDDING_PREFLIGHT") == "true":
        embeddings.main()
    if QUEUE_CONFIGURED:
        await broker.startup()
    try:
        yield
    finally:
        if QUEUE_CONFIGURED:
            await broker.shutdown()


observability.configure_logging()
app = FastAPI(title="Metis API", version="0.1.0", lifespan=lifespan)
app.middleware("http")(observability.request_metrics)


@app.exception_handler(knowledge.IndexUnavailable)
async def index_unavailable_handler(request, error):
    return JSONResponse(status_code=503, content={"detail": str(error)})


@app.get("/metrics", include_in_schema=False)
def metrics(session: Annotated[Session, Depends(get_session)]):
    from fastapi.responses import Response
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    observability.refresh_database_metrics(session)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def publish_job(session: Session, job: IngestionJob) -> None:
    try:
        await process_ingestion_job.kiq(str(job.id), str(job.organisation_id))
        observability.log_event(
            "job_published",
            job_id=str(job.id),
            organisation_id=str(job.organisation_id),
        )
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


@app.post("/api/auth/register", response_model=UserView, status_code=201)
def register_user(
    payload: UserRegister, session: Annotated[Session, Depends(get_session)]
) -> UserView:
    if session.scalar(select(User.id).where(User.email == payload.email)) is not None:
        raise HTTPException(status_code=409, detail="Email is already registered")
    user = User(
        email=payload.email,
        name=payload.name,
        password_hash=auth.hash_password(payload.password),
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Email is already registered"
        ) from error
    session.refresh(user)
    return user


@app.post("/api/auth/token", response_model=TokenView)
def issue_token(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: Annotated[Session, Depends(get_session)],
) -> TokenView:
    email = form.username.strip().casefold()
    user = session.scalar(select(User).where(User.email == email))
    password_valid = auth.verify_password(
        form.password, user.password_hash if user is not None else None
    )
    if user is None or not password_valid:
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        access_token = auth.create_access_token(user.id)
    except RuntimeError as error:
        raise HTTPException(
            status_code=503, detail="Authentication is not configured"
        ) from error
    return TokenView(access_token=access_token)


@app.get("/api/auth/me", response_model=CurrentUserView)
def current_user(
    user: Annotated[User, Depends(auth.get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> CurrentUserView:
    memberships = session.execute(
        select(OrganisationMembership, Organisation)
        .join(Organisation, Organisation.id == OrganisationMembership.organisation_id)
        .where(OrganisationMembership.user_id == user.id)
        .order_by(Organisation.name, Organisation.id)
    ).all()
    return CurrentUserView(
        id=user.id,
        email=user.email,
        name=user.name,
        created_at=user.created_at,
        memberships=[
            MembershipView(
                organisation_id=membership.organisation_id,
                organisation_name=organisation.name,
                role=membership.role,
            )
            for membership, organisation in memberships
        ],
    )


@app.post(
    "/api/organisations/{organisation_id}/claim",
    response_model=OrganisationView,
)
def claim_unowned_organisation(
    organisation_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(auth.get_current_user)],
    bootstrap_token: Annotated[
        str | None, Header(alias="X-Metis-Bootstrap-Token")
    ] = None,
) -> OrganisationView:
    auth.require_bootstrap_token(bootstrap_token)
    organisation = session.scalar(
        select(Organisation).where(Organisation.id == organisation_id).with_for_update()
    )
    if organisation is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    if (
        session.scalar(
            select(OrganisationMembership.user_id).where(
                OrganisationMembership.organisation_id == organisation_id
            )
        )
        is not None
    ):
        raise HTTPException(status_code=409, detail="Organisation already has members")
    session.add(
        OrganisationMembership(
            organisation_id=organisation_id, user_id=user.id, role="owner"
        )
    )
    audit.record_event(
        session,
        organisation_id,
        user.id,
        "organisation.claimed",
        "organisation",
        organisation_id,
    )
    session.commit()
    session.refresh(organisation)
    return organisation


@app.post("/api/organisations", response_model=OrganisationView, status_code=201)
def create_organisation(
    payload: OrganisationCreate,
    session: Annotated[Session, Depends(get_session)],
    user: Annotated[User, Depends(auth.get_current_user)],
) -> OrganisationView:
    organisation = Organisation(name=payload.name.strip())
    session.add(organisation)
    session.flush()
    session.add(
        OrganisationMembership(
            organisation_id=organisation.id, user_id=user.id, role="owner"
        )
    )
    audit.record_event(
        session,
        organisation.id,
        user.id,
        "organisation.created",
        "organisation",
        organisation.id,
    )
    session.commit()
    session.refresh(organisation)
    return organisation


@app.get(
    "/api/organisations/{organisation_id}/members",
    response_model=list[OrganisationMemberView],
)
def list_members(
    organisation_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_admin)],
) -> list[OrganisationMemberView]:
    rows = session.execute(
        select(OrganisationMembership, User)
        .join(User, User.id == OrganisationMembership.user_id)
        .where(OrganisationMembership.organisation_id == organisation_id)
        .order_by(OrganisationMembership.created_at, User.email)
    ).all()
    return [
        OrganisationMemberView(
            user_id=user.id,
            email=user.email,
            name=user.name,
            role=membership.role,
            created_at=membership.created_at,
        )
        for membership, user in rows
    ]


@app.post(
    "/api/organisations/{organisation_id}/members",
    response_model=OrganisationMemberView,
    status_code=201,
)
def add_member(
    organisation_id: UUID,
    payload: MemberAdd,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[OrganisationMembership, Depends(auth.require_admin)],
) -> OrganisationMemberView:
    if payload.role == "admin" and actor.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can add admins")
    user = session.scalar(select(User).where(User.email == payload.email))
    if user is None or user.password_hash is None:
        raise HTTPException(status_code=404, detail="Registered user not found")
    if session.get(OrganisationMembership, (organisation_id, user.id)) is not None:
        raise HTTPException(status_code=409, detail="User is already a member")
    membership = OrganisationMembership(
        organisation_id=organisation_id, user_id=user.id, role=payload.role
    )
    session.add(membership)
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "member.added",
        "user",
        user.id,
        {"role": payload.role},
    )
    session.commit()
    session.refresh(membership)
    return OrganisationMemberView(
        user_id=user.id,
        email=user.email,
        name=user.name,
        role=membership.role,
        created_at=membership.created_at,
    )


@app.get(
    "/api/organisations/{organisation_id}/audit-events",
    response_model=list[AuditEventView],
)
def list_audit_events(
    organisation_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_admin)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AuditEventView]:
    return audit.list_events(session, organisation_id, limit)


@app.post(
    "/api/organisations/{organisation_id}/sources",
    response_model=SourceView,
    status_code=201,
)
def create_source(
    organisation_id: UUID,
    payload: SourceCreate,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[OrganisationMembership, Depends(auth.require_admin)],
) -> SourceView:
    if payload.type == "microsoft365" and (
        set(payload.configuration) - {"sync_enabled"}
        or not isinstance(payload.configuration.get("sync_enabled", False), bool)
    ):
        raise HTTPException(
            status_code=422,
            detail="Microsoft 365 configuration accepts only sync_enabled; credentials are managed by the operator",
        )
    source = knowledge.create_source(
        session,
        organisation_id,
        payload.name,
        payload.type,
        payload.configuration,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "source.created",
        "source",
        source.id,
    )
    session.commit()
    return source


@app.post(
    "/api/organisations/{organisation_id}/sources/{source_id}/sync",
    response_model=SourceView,
    status_code=202,
)
async def synchronize_knowledge_source(
    organisation_id: UUID,
    source_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[OrganisationMembership, Depends(auth.require_admin)],
    full: bool = False,
) -> SourceView:
    from backend.services.source_sync import owned_source, source_is_busy

    source = owned_source(session, organisation_id, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    if source.type != "microsoft365":
        raise HTTPException(
            status_code=422, detail="This source does not support synchronization"
        )
    if not QUEUE_CONFIGURED:
        raise HTTPException(status_code=503, detail="Background queue is unavailable")
    if source_is_busy(source):
        return source
    if full:
        source.sync_checkpoint = None
    source.sync_status = "queued"
    source.sync_error = None
    session.commit()
    try:
        await synchronize_source.kiq(str(source.id), str(organisation_id))
    except Exception as error:
        source.sync_status = "failed"
        source.sync_error = "QueueUnavailable"
        session.commit()
        raise HTTPException(
            status_code=503, detail="Background queue is unavailable"
        ) from error
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "source.sync_requested",
        "source",
        source.id,
    )
    session.commit()
    return source


@app.get(
    "/api/organisations/{organisation_id}/sources",
    response_model=list[SourceView],
)
def list_sources(
    organisation_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_membership)],
) -> list[SourceView]:
    return knowledge.list_sources(session, organisation_id)


@app.post(
    "/api/organisations/{organisation_id}/documents",
    response_model=DocumentView,
    status_code=201,
)
def create_document(
    organisation_id: UUID,
    payload: DocumentCreate,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[OrganisationMembership, Depends(auth.require_admin)],
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
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "document.created",
        "document",
        document.id,
    )
    session.commit()
    return document


def document_view(session: Session, document) -> DocumentView:
    job = document_service.get_document_job(
        session, document.organisation_id, document.current_version_id
    )
    status = (
        "deleted"
        if document.deleted_at is not None
        else job.status
        if job is not None
        else None
    )
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
    actor: Annotated[OrganisationMembership, Depends(auth.require_admin)],
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

    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "document.uploaded",
        "document",
        document.id,
        {"job_id": str(job.id)},
    )
    session.commit()

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
    _: Annotated[OrganisationMembership, Depends(auth.require_membership)],
) -> DocumentView:
    document = knowledge.get_document(session, organisation_id, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return document_view(session, document)


@app.get(
    "/api/organisations/{organisation_id}/documents/{document_id}/versions/{version_id}/download"
)
def download_document_version(
    organisation_id: UUID,
    document_id: UUID,
    version_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_membership)],
) -> Response:
    from backend.models import DocumentVersion

    document = knowledge.get_document(session, organisation_id, document_id)
    version = session.scalar(
        select(DocumentVersion).where(
            DocumentVersion.id == version_id,
            DocumentVersion.document_id == document_id,
            DocumentVersion.organisation_id == organisation_id,
        )
    )
    if document is None or document.deleted_at is not None or version is None:
        raise HTTPException(status_code=404, detail="Document version not found")
    try:
        content = get_object_storage().get(version.object_key)
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=404, detail="Original file is unavailable"
        ) from error
    except ClientError as error:
        missing = error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}
        raise HTTPException(
            status_code=404 if missing else 503, detail="Original file is unavailable"
        ) from error
    except (OSError, BotoCoreError) as error:
        raise HTTPException(
            status_code=503, detail="Original file storage is unavailable"
        ) from error
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''"
            + quote(version.filename, safe=""),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post(
    "/api/organisations/{organisation_id}/documents/{document_id}/retry",
    response_model=JobView,
    status_code=202,
)
async def retry_document_ingestion(
    organisation_id: UUID,
    document_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    actor: Annotated[OrganisationMembership, Depends(auth.require_admin)],
) -> JobView:
    if not QUEUE_CONFIGURED:
        raise HTTPException(status_code=503, detail="Background queue is unavailable")
    if knowledge.get_document(session, organisation_id, document_id) is None:
        raise HTTPException(status_code=404, detail="Document not found")
    job = job_service.retry_failed_document_job(session, organisation_id, document_id)
    if job is None:
        raise HTTPException(
            status_code=409, detail="Document has no failed current ingestion"
        )
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "document.ingestion_retried",
        "document",
        document_id,
        {"job_id": str(job.id)},
    )
    session.commit()
    await publish_job(session, job)
    session.expire_all()
    return job_service.get_job(session, organisation_id, job.id) or job


@app.get(
    "/api/organisations/{organisation_id}/documents",
    response_model=list[DocumentView],
)
def list_documents(
    organisation_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_membership)],
) -> list[DocumentView]:
    return [
        document_view(session, document)
        for document in knowledge.list_documents(session, organisation_id)
    ]


@app.get(
    "/api/organisations/{organisation_id}/groups",
    response_model=list[KnowledgeGroupView],
)
def list_knowledge_groups(
    organisation_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_membership)],
):
    from backend.models import KnowledgeGroup

    return [
        groups.view(session, group)
        for group in session.scalars(
            select(KnowledgeGroup)
            .where(KnowledgeGroup.organisation_id == organisation_id)
            .order_by(KnowledgeGroup.name)
        )
    ]


@app.post(
    "/api/organisations/{organisation_id}/groups",
    response_model=KnowledgeGroupView,
    status_code=201,
)
def create_knowledge_group(
    organisation_id: UUID,
    payload: KnowledgeGroupWrite,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_admin)],
):
    try:
        return groups.save(session, organisation_id, payload.name, payload.source_ids)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.put(
    "/api/organisations/{organisation_id}/groups/{group_id}",
    response_model=KnowledgeGroupView,
)
def update_knowledge_group(
    organisation_id: UUID,
    group_id: UUID,
    payload: KnowledgeGroupWrite,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_admin)],
):
    try:
        return groups.save(
            session, organisation_id, payload.name, payload.source_ids, group_id
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.delete("/api/organisations/{organisation_id}/groups/{group_id}", status_code=204)
def delete_knowledge_group(
    organisation_id: UUID,
    group_id: UUID,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_admin)],
):
    group = groups.owned_group(session, organisation_id, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Knowledge group not found")
    session.delete(group)
    session.commit()


def request_scope(
    session: Session, organisation_id: UUID, payload: SearchRequest | ChatRequest
):
    sources = payload.source_ids
    documents = payload.document_ids
    if payload.group_id is not None:
        if sources is not None or payload.source_id is not None:
            raise HTTPException(status_code=422, detail="Select a group or source IDs")
        try:
            sources = groups.source_ids(session, organisation_id, payload.group_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    for identifier, selected in (
        (payload.source_id, sources),
        (payload.document_id, documents),
    ):
        if (
            identifier is not None
            and selected is not None
            and identifier not in selected
        ):
            raise HTTPException(status_code=422, detail="Conflicting knowledge scope")
    sources = (
        sources
        if sources is not None
        else ([payload.source_id] if payload.source_id else None)
    )
    documents = (
        documents
        if documents is not None
        else ([payload.document_id] if payload.document_id else None)
    )
    try:
        knowledge.validate_scope(session, organisation_id, sources, documents)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return sources, documents


@app.post("/api/organisations/{organisation_id}/search", response_model=SearchView)
def search_documents(
    organisation_id: UUID,
    payload: SearchRequest,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[OrganisationMembership, Depends(auth.require_membership)],
) -> SearchView:
    if session.get(Organisation, organisation_id) is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    source_ids, document_ids = request_scope(session, organisation_id, payload)
    try:
        provider = embeddings.get_embedding_provider()
        query_embedding = provider.embed_query(payload.query)
    except embeddings.InvalidEmbeddingInput as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (RuntimeError, OSError, TypeError, ValueError) as error:
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
        source_ids=source_ids,
        document_ids=document_ids,
        query_text=payload.query,
    )
    return SearchView(
        embedding_model=provider.model_id,
        results=[
            SearchResultView(
                document_version_id=hit.chunk.document_version_id,
                source_modified_at=hit.document.external_modified_at,
                source_url=hit.document.source_uri,
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
        document_version_id=hit.chunk.document_version_id,
        source_modified_at=hit.document.external_modified_at,
        chunk_id=hit.chunk.id,
        document_id=hit.document.id,
        document_title=hit.document.title,
        source_url=hit.document.source_uri,
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
    actor: Annotated[OrganisationMembership, Depends(auth.require_membership)],
) -> ChatResponse:
    if session.get(Organisation, organisation_id) is None:
        raise HTTPException(status_code=404, detail="Organisation not found")

    source_ids, document_ids = request_scope(session, organisation_id, payload)
    conversation = None
    conversation_id = payload.conversation_id or uuid4()
    if payload.conversation_id is not None:
        conversation = conversations.get_conversation(
            session,
            organisation_id,
            payload.conversation_id,
            actor.user_id,
            actor.role,
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
    except (RuntimeError, OSError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=503, detail="Embedding provider is unavailable"
        ) from error

    hits = knowledge.search_chunks(
        session,
        organisation_id,
        query_embedding,
        embedding_provider.model_id,
        payload.top_k,
        document_id=payload.document_id,
        source_ids=source_ids,
        document_ids=document_ids,
        expand_neighbors=True,
        query_text=payload.message,
    )
    complete_scope = False
    if rag.needs_complete_scope(payload.message):
        complete = knowledge.complete_scoped_evidence(
            session,
            organisation_id,
            embedding_provider.model_id,
            source_ids,
            document_ids,
        )
        if complete is not None:
            hits = complete
            complete_scope = True
    session.commit()
    has_evidence = bool(
        rag.supporting_evidence(hits, payload.document_id, document_ids)
    )
    try:
        chat_provider = (
            chat.get_chat_provider()
            if has_evidence
            and (not rag.needs_complete_scope(payload.message) or complete_scope)
            else None
        )
        grounded = rag.answer_question(
            payload.message,
            history,
            hits,
            chat_provider,
            document_id=payload.document_id,
            document_ids=document_ids,
            complete_scope=complete_scope,
        )
    except chat.ChatProviderError as error:
        raise HTTPException(
            status_code=503, detail="Chat provider is unavailable"
        ) from error

    citations = [citation_view(hit) for hit in grounded.citations]
    completion = grounded.completion
    conversations.save_turn(
        session,
        organisation_id,
        actor.user_id,
        conversation,
        conversation_id,
        payload.message,
        grounded.content,
        [citation.model_dump(mode="json") for citation in citations],
        completion.model_id if completion else None,
        completion.input_tokens if completion else None,
        completion.output_tokens if completion else None,
    )
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "chat.turn",
        "conversation",
        conversation_id,
        {
            "citation_count": len(citations),
            "model_id": completion.model_id if completion else None,
        },
    )
    session.commit()
    return ChatResponse(
        conversation_id=conversation_id,
        answer=grounded.content,
        outcome=grounded.outcome,
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
    actor: Annotated[OrganisationMembership, Depends(auth.require_membership)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> ConversationView:
    conversation = conversations.get_conversation(
        session, organisation_id, conversation_id, actor.user_id, actor.role
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
    user: Annotated[User, Depends(auth.get_current_user)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=255)
    ] = None,
) -> JobView:
    membership = session.get(OrganisationMembership, (payload.organisation_id, user.id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    if membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organisation admin role required")
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
    _: Annotated[OrganisationMembership, Depends(auth.require_membership)],
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
