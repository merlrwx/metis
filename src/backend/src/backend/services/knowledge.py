import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend import observability
from backend.models import Chunk, Document, DocumentVersion, Organisation, Source


@dataclass(frozen=True)
class SearchHit:
    chunk: Chunk
    document: Document
    source: Source | None
    score: float


def create_organisation(session: Session, name: str) -> Organisation:
    organisation = Organisation(name=name.strip())
    session.add(organisation)
    session.commit()
    session.refresh(organisation)
    return organisation


def create_source(
    session: Session,
    organisation_id: uuid.UUID,
    name: str,
    source_type: str,
    configuration: dict,
) -> Source | None:
    if session.get(Organisation, organisation_id) is None:
        return None
    source = Source(
        organisation_id=organisation_id,
        name=name.strip(),
        type=source_type,
        configuration=configuration,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def list_sources(session: Session, organisation_id: uuid.UUID) -> list[Source]:
    return list(
        session.scalars(
            select(Source)
            .where(Source.organisation_id == organisation_id)
            .order_by(Source.created_at, Source.id)
        )
    )


def create_document(
    session: Session,
    organisation_id: uuid.UUID,
    title: str,
    source_id: uuid.UUID | None,
    source_uri: str | None,
) -> Document | None:
    if session.get(Organisation, organisation_id) is None:
        return None
    if (
        source_id is not None
        and session.scalar(
            select(Source.id).where(
                Source.id == source_id, Source.organisation_id == organisation_id
            )
        )
        is None
    ):
        return None
    document = Document(
        organisation_id=organisation_id,
        title=title.strip(),
        source_id=source_id,
        source_uri=source_uri,
    )
    session.add(document)
    session.commit()
    session.refresh(document)
    return document


def list_documents(session: Session, organisation_id: uuid.UUID) -> list[Document]:
    return list(
        session.scalars(
            select(Document)
            .where(Document.organisation_id == organisation_id)
            .order_by(Document.created_at.desc(), Document.id)
        )
    )


def get_document(
    session: Session, organisation_id: uuid.UUID, document_id: uuid.UUID
) -> Document | None:
    return session.scalar(
        select(Document).where(
            Document.organisation_id == organisation_id,
            Document.id == document_id,
        )
    )


class IndexUnavailable(RuntimeError):
    pass


def ensure_index_ready(
    session: Session,
    organisation_id: uuid.UUID,
    model_id: str | None = None,
    *,
    lock: bool = False,
) -> None:
    statement = (
        select(Organisation)
        .where(Organisation.id == organisation_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    organisation = session.scalar(statement)
    if organisation is not None and (
        organisation.index_status != "ready"
        or (
            model_id is not None
            and organisation.index_model is not None
            and organisation.index_model != model_id
        )
    ):
        raise IndexUnavailable(
            "Knowledge is being re-indexed or uses another embedding model. Ask an administrator to check indexing status."
        )
    if (
        organisation is not None
        and organisation.index_model is None
        and model_id is not None
    ):
        models = set(
            session.scalars(
                select(Chunk.embedding_model)
                .join(
                    Document, Document.current_version_id == Chunk.document_version_id
                )
                .where(
                    Document.organisation_id == organisation_id,
                    Chunk.organisation_id == organisation_id,
                )
            )
        )
        if models and models != {model_id}:
            raise IndexUnavailable(
                "Knowledge uses another embedding model; re-index the organisation before searching or uploading."
            )


@observability.RETRIEVAL_DURATION.time()
def search_chunks(
    session: Session,
    organisation_id: uuid.UUID,
    query_embedding: list[float],
    embedding_model: str,
    limit: int,
    source_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
) -> list[SearchHit]:
    ensure_index_ready(session, organisation_id, embedding_model)
    distance = Chunk.embedding.cosine_distance(query_embedding)
    statement = (
        select(Chunk, Document, Source, distance.label("distance"))
        .join(
            DocumentVersion,
            (DocumentVersion.id == Chunk.document_version_id)
            & (DocumentVersion.organisation_id == Chunk.organisation_id),
        )
        .join(
            Document,
            (Document.id == DocumentVersion.document_id)
            & (Document.organisation_id == Chunk.organisation_id),
        )
        .outerjoin(
            Source,
            (Source.id == Document.source_id)
            & (Source.organisation_id == Document.organisation_id),
        )
        .where(
            Chunk.organisation_id == organisation_id,
            Document.organisation_id == organisation_id,
            Document.current_version_id == Chunk.document_version_id,
            Chunk.embedding_model == embedding_model,
        )
    )
    if source_id is not None:
        statement = statement.where(Document.source_id == source_id)
    if document_id is not None:
        statement = statement.where(Document.id == document_id)

    rows = session.execute(statement.order_by(distance).limit(limit))
    return [
        SearchHit(
            chunk=chunk,
            document=document,
            source=source,
            score=1 - distance_value,
        )
        for chunk, document, source, distance_value in rows
    ]
