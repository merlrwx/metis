import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Chunk, Conversation, Document, Message, Source
from backend.schemas import CitationView


def get_conversation(
    session: Session,
    organisation_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
) -> Conversation | None:
    statement = select(Conversation).where(
        Conversation.organisation_id == organisation_id,
        Conversation.id == conversation_id,
    )
    if role not in {"owner", "admin"}:
        statement = statement.where(Conversation.user_id == user_id)
    return session.scalar(statement)


def get_history(
    session: Session,
    organisation_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int,
    source_ids: list[uuid.UUID] | None = None,
    document_ids: list[uuid.UUID] | None = None,
) -> list[tuple[str, str]]:
    rows = session.scalars(
        select(Message)
        .where(
            Message.organisation_id == organisation_id,
            Message.conversation_id == conversation_id,
        )
        .order_by(Message.message_index.desc())
        .limit(limit)
    ).all()
    return [
        (message.role, message.content)
        for message in reversed(rows)
        if message.role == "user"
        or citations_available(
            session, organisation_id, message.citations, source_ids, document_ids
        )
    ]


def save_turn(
    session: Session,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation: Conversation | None,
    conversation_id: uuid.UUID,
    question: str,
    answer: str,
    citations: list[dict],
    model_id: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    scope: dict | None = None,
    outcome: str | None = None,
) -> Conversation:
    if conversation is None:
        conversation = Conversation(
            id=conversation_id,
            organisation_id=organisation_id,
            user_id=user_id,
            title=question[:512],
        )
        session.add(conversation)
        next_message_index = 0
    else:
        conversation = session.scalar(
            select(Conversation)
            .where(
                Conversation.organisation_id == organisation_id,
                Conversation.id == conversation_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise ValueError("Conversation does not belong to the organisation")
        last_message_index = session.scalar(
            select(func.max(Message.message_index)).where(
                Message.organisation_id == organisation_id,
                Message.conversation_id == conversation_id,
            )
        )
        next_message_index = (
            last_message_index if last_message_index is not None else -1
        ) + 1

    if scope is not None:
        conversation.scope = scope
    session.add_all(
        [
            Message(
                organisation_id=organisation_id,
                conversation_id=conversation_id,
                message_index=next_message_index,
                role="user",
                content=question,
            ),
            Message(
                organisation_id=organisation_id,
                conversation_id=conversation_id,
                message_index=next_message_index + 1,
                role="assistant",
                outcome=outcome,
                content=answer,
                citations=citations,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        ]
    )
    session.flush()
    return conversation


def list_messages(
    session: Session,
    organisation_id: uuid.UUID,
    conversation_id: uuid.UUID,
    limit: int,
) -> list[Message]:
    rows = session.scalars(
        select(Message)
        .where(
            Message.organisation_id == organisation_id,
            Message.conversation_id == conversation_id,
        )
        .order_by(Message.message_index.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))


def citations_available(
    session: Session,
    organisation_id: uuid.UUID,
    citations: list[dict],
    source_ids: list[uuid.UUID] | None = None,
    document_ids: list[uuid.UUID] | None = None,
) -> bool:
    for citation in citations:
        try:
            chunk_id = uuid.UUID(str(citation["chunk_id"]))
            document_id = uuid.UUID(str(citation["document_id"]))
        except (ValueError, KeyError, TypeError):
            return False
        statement = (
            select(Chunk.id)
            .join(
                Document,
                (Document.current_version_id == Chunk.document_version_id)
                & (Document.organisation_id == Chunk.organisation_id),
            )
            .where(
                Chunk.id == chunk_id,
                Chunk.organisation_id == organisation_id,
                Document.id == document_id,
                Document.organisation_id == organisation_id,
                Document.deleted_at.is_(None),
            )
        )
        if source_ids is not None:
            statement = statement.where(Document.source_id.in_(source_ids))
        if document_ids is not None:
            statement = statement.where(Document.id.in_(document_ids))
        if session.scalar(statement) is None:
            return False
    return True


def accessible_conversations(
    session: Session,
    organisation_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    limit: int,
    offset: int,
) -> list[Conversation]:
    statement = select(Conversation).where(
        Conversation.organisation_id == organisation_id
    )
    if role not in {"owner", "admin"}:
        statement = statement.where(Conversation.user_id == user_id)
    return list(
        session.scalars(
            statement.order_by(Conversation.created_at.desc(), Conversation.id)
            .offset(offset)
            .limit(limit)
        )
    )


def refresh_citation_sources(
    session: Session, organisation_id: uuid.UUID, citations: list[CitationView]
) -> list[CitationView]:
    identifiers = {citation.source_id for citation in citations if citation.source_id}
    if not identifiers:
        return citations
    sources = {
        source.id: source
        for source in session.scalars(
            select(Source).where(
                Source.organisation_id == organisation_id, Source.id.in_(identifiers)
            )
        )
    }
    result = []
    for citation in citations:
        source = sources.get(citation.source_id)
        if source is not None:
            citation = citation.model_copy(
                update={
                    "source_type": source.type,
                    "source_freshness": source_freshness(source),
                    "source_sync_status": "paused"
                    if source.configuration.get("sync_paused")
                    else source.sync_status,
                    "source_last_synced_at": source.last_synced_at,
                }
            )
        result.append(citation)
    return result


def source_freshness(source: Source) -> str:
    if source.configuration.get("sync_paused"):
        return "paused"
    if source.sync_status == "failed":
        return "failed"
    if source.last_synced_at is None:
        return "not_synced"
    if not source.configuration.get("sync_enabled"):
        return "snapshot"
    last_sync = source.last_synced_at
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=UTC)
    return (
        "stale"
        if datetime.now(UTC) - last_sync > timedelta(minutes=30)
        else "recent_snapshot"
    )
