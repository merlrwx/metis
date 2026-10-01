import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Document, Organisation, Source


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
