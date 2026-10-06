from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.models import KnowledgeGroup, KnowledgeGroupSource
from backend.schemas import KnowledgeGroupView
from backend.services.knowledge import validate_scope


def owned_group(session: Session, organisation_id: UUID, group_id: UUID):
    return session.scalar(
        select(KnowledgeGroup).where(
            KnowledgeGroup.organisation_id == organisation_id,
            KnowledgeGroup.id == group_id,
        )
    )


def source_ids(session: Session, organisation_id: UUID, group_id: UUID) -> list[UUID]:
    if owned_group(session, organisation_id, group_id) is None:
        raise ValueError("Knowledge group not found")
    return list(
        session.scalars(
            select(KnowledgeGroupSource.source_id).where(
                KnowledgeGroupSource.organisation_id == organisation_id,
                KnowledgeGroupSource.group_id == group_id,
            )
        )
    )


def view(session: Session, group: KnowledgeGroup) -> KnowledgeGroupView:
    return KnowledgeGroupView(
        id=group.id,
        name=group.name,
        source_ids=source_ids(session, group.organisation_id, group.id),
    )


def save(
    session: Session,
    organisation_id: UUID,
    name: str,
    selected: list[UUID],
    group_id: UUID | None = None,
):
    validate_scope(session, organisation_id, selected, None)
    group = (
        owned_group(session, organisation_id, group_id)
        if group_id
        else KnowledgeGroup(organisation_id=organisation_id, name=name)
    )
    if group is None:
        raise ValueError("Knowledge group not found")
    group.name = name
    session.add(group)
    session.flush()
    session.execute(
        delete(KnowledgeGroupSource).where(
            KnowledgeGroupSource.group_id == group.id,
            KnowledgeGroupSource.organisation_id == organisation_id,
        )
    )
    session.add_all(
        [
            KnowledgeGroupSource(
                group_id=group.id, organisation_id=organisation_id, source_id=identifier
            )
            for identifier in set(selected)
        ]
    )
    session.commit()
    return view(session, group)
