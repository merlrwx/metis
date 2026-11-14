"""Registration, sign-in sessions and account self-service."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import auth
from backend.database import get_session
from backend.models import (
    AuthSession,
    Organisation,
    OrganisationMembership,
    User,
)
from backend.schemas import (
    CurrentUserView,
    MembershipView,
    PasswordChange,
    ProfileUpdate,
    RefreshRequest,
    SessionView,
    TokenView,
    UserRegister,
    UserView,
)

router = APIRouter(prefix="/api/auth", tags=["account"])
CurrentAuth = Annotated[tuple[User, AuthSession], Depends(auth.get_current_auth)]
DatabaseSession = Annotated[Session, Depends(get_session)]


def _token_or_503(auth_session: AuthSession, refresh_token: str) -> TokenView:
    try:
        return TokenView(**auth.token_response(auth_session, refresh_token))
    except RuntimeError as error:
        raise HTTPException(
            status_code=503, detail="Authentication is not configured"
        ) from error


@router.post("/register", response_model=UserView, status_code=201)
def register_user(payload: UserRegister, session: DatabaseSession) -> UserView:
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


@router.post("/token", response_model=TokenView)
def issue_token(
    request: Request,
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: DatabaseSession,
) -> TokenView:
    email = form.username.strip().casefold()
    auth.ensure_login_allowed(session, email)
    user = session.scalar(select(User).where(User.email == email))
    password_valid = auth.verify_password(
        form.password, user.password_hash if user is not None else None
    )
    if user is None or not password_valid:
        auth.record_login_failure(session, email)
        raise HTTPException(
            status_code=401,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    auth.clear_login_failures(session, email)
    auth_session, refresh_token = auth.start_session(
        session,
        user,
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    tokens = _token_or_503(auth_session, refresh_token)
    session.commit()
    return tokens


@router.post("/refresh", response_model=TokenView)
def refresh_token(payload: RefreshRequest, session: DatabaseSession) -> TokenView:
    auth_session, new_refresh_token = auth.rotate_refresh_token(
        session, payload.refresh_token
    )
    tokens = _token_or_503(auth_session, new_refresh_token)
    session.commit()
    return tokens


@router.post("/logout", status_code=204)
def logout(current: CurrentAuth, session: DatabaseSession) -> Response:
    _, auth_session = current
    auth_session.revoked_at = auth.utcnow()
    session.commit()
    return Response(status_code=204)


def _current_user_view(session: Session, user: User) -> CurrentUserView:
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


@router.get("/me", response_model=CurrentUserView)
def current_user(
    user: Annotated[User, Depends(auth.get_current_user)], session: DatabaseSession
) -> CurrentUserView:
    return _current_user_view(session, user)


@router.patch("/me", response_model=CurrentUserView)
def update_profile(
    payload: ProfileUpdate,
    user: Annotated[User, Depends(auth.get_current_user)],
    session: DatabaseSession,
) -> CurrentUserView:
    user.name = payload.name
    session.commit()
    return _current_user_view(session, user)


@router.post("/password", status_code=204)
def change_password(
    payload: PasswordChange, current: CurrentAuth, session: DatabaseSession
) -> Response:
    user, auth_session = current
    if not auth.verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=400, detail="Choose a password you have not just used"
        )
    user.password_hash = auth.hash_password(payload.new_password)
    auth.revoke_sessions(session, user.id, keep=auth_session.id)
    session.commit()
    return Response(status_code=204)


@router.get("/sessions", response_model=list[SessionView])
def list_sessions(current: CurrentAuth, session: DatabaseSession) -> list[SessionView]:
    user, auth_session = current
    rows = session.scalars(
        select(AuthSession)
        .where(
            AuthSession.user_id == user.id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > auth.utcnow(),
        )
        .order_by(AuthSession.last_used_at.desc(), AuthSession.id)
    )
    return [
        SessionView(
            id=row.id,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            user_agent=row.user_agent,
            ip_address=row.ip_address,
            current=row.id == auth_session.id,
        )
        for row in rows
    ]


@router.post("/sessions/revoke-others", status_code=204)
def revoke_other_sessions(current: CurrentAuth, session: DatabaseSession) -> Response:
    user, auth_session = current
    auth.revoke_sessions(session, user.id, keep=auth_session.id)
    session.commit()
    return Response(status_code=204)


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(
    session_id: UUID, current: CurrentAuth, session: DatabaseSession
) -> Response:
    user, _ = current
    target = session.get(AuthSession, session_id)
    if target is None or target.user_id != user.id or target.revoked_at is not None:
        raise HTTPException(status_code=404, detail="Session not found")
    target.revoked_at = auth.utcnow()
    session.commit()
    return Response(status_code=204)
