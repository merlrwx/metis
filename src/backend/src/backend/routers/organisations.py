"""Organisations, memberships, invitations and audit history."""

import os
import secrets
from datetime import timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend import auth
from backend.database import get_session
from backend.models import Invitation, Organisation, OrganisationMembership, User
from backend.schemas import (
    AuditEventView,
    InvitationAccept,
    InvitationCreate,
    InvitationCreatedView,
    InvitationPreview,
    InvitationView,
    MemberAdd,
    MemberRoleUpdate,
    OrganisationCreate,
    OrganisationMemberView,
    OrganisationView,
)
from backend.services import audit

router = APIRouter(tags=["organisations"])
DatabaseSession = Annotated[Session, Depends(get_session)]
CurrentUser = Annotated[User, Depends(auth.get_current_user)]
Admin = Annotated[OrganisationMembership, Depends(auth.require_admin)]
Owner = Annotated[OrganisationMembership, Depends(auth.require_owner)]
Member = Annotated[OrganisationMembership, Depends(auth.require_membership)]


def invitation_lifetime() -> timedelta:
    days = int(os.environ.get("METIS_INVITATION_DAYS", "7"))
    if not 1 <= days <= 30:
        raise RuntimeError("METIS_INVITATION_DAYS must be between 1 and 30")
    return timedelta(days=days)


def _member_view(membership: OrganisationMembership, user: User):
    return OrganisationMemberView(
        user_id=user.id,
        email=user.email,
        name=user.name,
        role=membership.role,
        created_at=membership.created_at,
    )


def _locked_organisation(session: Session, organisation_id: UUID) -> Organisation:
    # Serialise membership changes so concurrent demotions cannot remove every owner.
    organisation = session.scalar(
        select(Organisation).where(Organisation.id == organisation_id).with_for_update()
    )
    if organisation is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    return organisation


def _owner_count(session: Session, organisation_id: UUID) -> int:
    return session.scalar(
        select(func.count()).where(
            OrganisationMembership.organisation_id == organisation_id,
            OrganisationMembership.role == "owner",
        )
    )


@router.post(
    "/api/organisations/{organisation_id}/claim", response_model=OrganisationView
)
def claim_unowned_organisation(
    organisation_id: UUID,
    session: DatabaseSession,
    user: CurrentUser,
    bootstrap_token: Annotated[
        str | None, Header(alias="X-Metis-Bootstrap-Token")
    ] = None,
) -> OrganisationView:
    auth.require_bootstrap_token(bootstrap_token)
    organisation = _locked_organisation(session, organisation_id)
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


@router.post("/api/organisations", response_model=OrganisationView, status_code=201)
def create_organisation(
    payload: OrganisationCreate, session: DatabaseSession, user: CurrentUser
) -> OrganisationView:
    organisation = Organisation(name=payload.name)
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


@router.patch("/api/organisations/{organisation_id}", response_model=OrganisationView)
def rename_organisation(
    organisation_id: UUID,
    payload: OrganisationCreate,
    session: DatabaseSession,
    actor: Admin,
) -> OrganisationView:
    organisation = _locked_organisation(session, organisation_id)
    organisation.name = payload.name
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "organisation.renamed",
        "organisation",
        organisation_id,
    )
    session.commit()
    session.refresh(organisation)
    return organisation


@router.get(
    "/api/organisations/{organisation_id}/members",
    response_model=list[OrganisationMemberView],
)
def list_members(
    organisation_id: UUID, session: DatabaseSession, _: Admin
) -> list[OrganisationMemberView]:
    rows = session.execute(
        select(OrganisationMembership, User)
        .join(User, User.id == OrganisationMembership.user_id)
        .where(OrganisationMembership.organisation_id == organisation_id)
        .order_by(OrganisationMembership.created_at, User.email)
    ).all()
    return [_member_view(membership, user) for membership, user in rows]


@router.post(
    "/api/organisations/{organisation_id}/members",
    response_model=OrganisationMemberView,
    status_code=201,
)
def add_member(
    organisation_id: UUID, payload: MemberAdd, session: DatabaseSession, actor: Admin
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
    return _member_view(membership, user)


@router.patch(
    "/api/organisations/{organisation_id}/members/{user_id}",
    response_model=OrganisationMemberView,
)
def change_member_role(
    organisation_id: UUID,
    user_id: UUID,
    payload: MemberRoleUpdate,
    session: DatabaseSession,
    actor: Owner,
) -> OrganisationMemberView:
    _locked_organisation(session, organisation_id)
    membership = session.get(OrganisationMembership, (organisation_id, user_id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Member not found")
    previous = membership.role
    if (
        previous == "owner"
        and payload.role != "owner"
        and _owner_count(session, organisation_id) <= 1
    ):
        raise HTTPException(
            status_code=409, detail="An organisation needs at least one owner"
        )
    membership.role = payload.role
    if previous != payload.role:
        audit.record_event(
            session,
            organisation_id,
            actor.user_id,
            "member.role_changed",
            "user",
            user_id,
            {"from": previous, "to": payload.role},
        )
    session.commit()
    return _member_view(membership, session.get(User, user_id))


@router.delete(
    "/api/organisations/{organisation_id}/members/{user_id}", status_code=204
)
def remove_member(
    organisation_id: UUID, user_id: UUID, session: DatabaseSession, actor: Member
) -> Response:
    _locked_organisation(session, organisation_id)
    target = session.get(OrganisationMembership, (organisation_id, user_id))
    if target is None:
        raise HTTPException(status_code=404, detail="Member not found")
    leaving = user_id == actor.user_id
    if not leaving:
        if actor.role not in {"owner", "admin"}:
            raise HTTPException(
                status_code=403, detail="Organisation admin role required"
            )
        if target.role in {"owner", "admin"} and actor.role != "owner":
            raise HTTPException(
                status_code=403, detail="Only an owner can remove owners or admins"
            )
    if target.role == "owner" and _owner_count(session, organisation_id) <= 1:
        raise HTTPException(
            status_code=409, detail="An organisation needs at least one owner"
        )
    session.delete(target)
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "member.left" if leaving else "member.removed",
        "user",
        user_id,
        {"role": target.role},
    )
    session.commit()
    return Response(status_code=204)


def _pending_invitations(organisation_id: UUID):
    return select(Invitation).where(
        Invitation.organisation_id == organisation_id,
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > auth.utcnow(),
    )


@router.get(
    "/api/organisations/{organisation_id}/invitations",
    response_model=list[InvitationView],
)
def list_invitations(
    organisation_id: UUID, session: DatabaseSession, _: Admin
) -> list[InvitationView]:
    return list(
        session.scalars(
            _pending_invitations(organisation_id).order_by(
                Invitation.created_at.desc(), Invitation.id
            )
        )
    )


@router.post(
    "/api/organisations/{organisation_id}/invitations",
    response_model=InvitationCreatedView,
    status_code=201,
)
def create_invitation(
    organisation_id: UUID,
    payload: InvitationCreate,
    session: DatabaseSession,
    actor: Admin,
) -> InvitationCreatedView:
    if payload.role == "admin" and actor.role != "owner":
        raise HTTPException(status_code=403, detail="Only an owner can invite admins")
    existing_member = session.scalar(
        select(OrganisationMembership.user_id)
        .join(User, User.id == OrganisationMembership.user_id)
        .where(
            OrganisationMembership.organisation_id == organisation_id,
            User.email == payload.email,
        )
    )
    if existing_member is not None:
        raise HTTPException(status_code=409, detail="User is already a member")
    now = auth.utcnow()
    # Re-inviting replaces any earlier pending link for the same address.
    session.execute(
        update(Invitation)
        .where(
            Invitation.organisation_id == organisation_id,
            Invitation.email == payload.email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        organisation_id=organisation_id,
        email=payload.email,
        role=payload.role,
        token_hash=auth.secret_hash(token),
        invited_by_user_id=actor.user_id,
        created_at=now,
        expires_at=now + invitation_lifetime(),
    )
    session.add(invitation)
    session.flush()
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "invitation.created",
        "invitation",
        invitation.id,
        {"role": payload.role},
    )
    session.commit()
    view = InvitationView.model_validate(invitation)
    return InvitationCreatedView(**view.model_dump(), token=token)


@router.delete(
    "/api/organisations/{organisation_id}/invitations/{invitation_id}",
    status_code=204,
)
def revoke_invitation(
    organisation_id: UUID, invitation_id: UUID, session: DatabaseSession, actor: Admin
) -> Response:
    invitation = session.scalar(
        _pending_invitations(organisation_id).where(Invitation.id == invitation_id)
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    invitation.revoked_at = auth.utcnow()
    audit.record_event(
        session,
        organisation_id,
        actor.user_id,
        "invitation.revoked",
        "invitation",
        invitation.id,
    )
    session.commit()
    return Response(status_code=204)


def _pending_by_token(session: Session, token: str, lock: bool = False):
    statement = select(Invitation).where(
        Invitation.token_hash == auth.secret_hash(token),
        Invitation.accepted_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > auth.utcnow(),
    )
    if lock:
        statement = statement.with_for_update()
    invitation = session.scalar(statement)
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found or expired")
    return invitation


@router.post("/api/invitations/preview", response_model=InvitationPreview)
def preview_invitation(
    payload: InvitationAccept, session: DatabaseSession
) -> InvitationPreview:
    invitation = _pending_by_token(session, payload.token)
    organisation = session.get(Organisation, invitation.organisation_id)
    return InvitationPreview(
        organisation_name=organisation.name,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
    )


@router.post("/api/invitations/accept", response_model=InvitationView)
def accept_invitation(
    payload: InvitationAccept, session: DatabaseSession, user: CurrentUser
) -> InvitationView:
    invitation = _pending_by_token(session, payload.token, lock=True)
    if invitation.email != user.email:
        raise HTTPException(
            status_code=403,
            detail="This invitation was sent to a different email address",
        )
    if (
        session.get(OrganisationMembership, (invitation.organisation_id, user.id))
        is None
    ):
        session.add(
            OrganisationMembership(
                organisation_id=invitation.organisation_id,
                user_id=user.id,
                role=invitation.role,
            )
        )
    invitation.accepted_at = auth.utcnow()
    invitation.accepted_by_user_id = user.id
    audit.record_event(
        session,
        invitation.organisation_id,
        user.id,
        "invitation.accepted",
        "invitation",
        invitation.id,
        {"role": invitation.role},
    )
    session.commit()
    return invitation


@router.get(
    "/api/organisations/{organisation_id}/audit-events",
    response_model=list[AuditEventView],
)
def list_audit_events(
    organisation_id: UUID,
    session: DatabaseSession,
    _: Admin,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[AuditEventView]:
    return audit.list_events(session, organisation_id, limit)
