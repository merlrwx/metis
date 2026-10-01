import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models import Conversation, Message


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
    return [(message.role, message.content) for message in reversed(rows)]


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
                content=answer,
                citations=citations,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        ]
    )
    session.commit()
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
