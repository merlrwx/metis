import hashlib
import hmac
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from backend.database import get_session
from backend.models import AuthAttempt, AuthSession, OrganisationMembership, User

password_hash = PasswordHash.recommended()
dummy_password_hash = password_hash.hash("invalid-login-placeholder-password")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")
algorithm = "HS256"
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def access_token_lifetime() -> timedelta:
    return timedelta(minutes=_bounded_int("METIS_ACCESS_TOKEN_MINUTES", 30, 1, 60))


def session_lifetime() -> timedelta:
    return timedelta(days=_bounded_int("METIS_SESSION_DAYS", 30, 1, 365))


def login_failure_limit() -> int:
    return _bounded_int("METIS_LOGIN_MAX_FAILURES", 10, 3, 1000)


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; every stored timestamp is UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str | None) -> bool:
    if hashed_password is None:
        password_hash.verify(password, dummy_password_hash)
        return False
    return password_hash.verify(password, hashed_password)


def _secret_key() -> str:
    key = os.environ.get("METIS_AUTH_SECRET_KEY", "")
    if len(key) < 32:
        raise RuntimeError("METIS_AUTH_SECRET_KEY must contain at least 32 characters")
    return key


def create_access_token(user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    now = utcnow()
    return jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "typ": "access",
            "iat": now,
            "exp": now + access_token_lifetime(),
        },
        _secret_key(),
        algorithm=algorithm,
    )


def _new_refresh_token(session_id: uuid.UUID) -> str:
    # The session id prefix lets a reused (rotated-out) token identify its session.
    return f"{session_id}.{secrets.token_urlsafe(32)}"


def start_session(
    session: Session,
    user: User,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[AuthSession, str]:
    auth_session = AuthSession(
        id=uuid.uuid4(),
        user_id=user.id,
        refresh_token_hash="",
        expires_at=utcnow() + session_lifetime(),
        last_used_at=utcnow(),
        user_agent=(user_agent or "")[:255] or None,
        ip_address=(ip_address or "")[:64] or None,
    )
    refresh_token = _new_refresh_token(auth_session.id)
    auth_session.refresh_token_hash = secret_hash(refresh_token)
    session.add(auth_session)
    return auth_session, refresh_token


def token_response(auth_session: AuthSession, refresh_token: str) -> dict:
    return {
        "access_token": create_access_token(auth_session.user_id, auth_session.id),
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": int(access_token_lifetime().total_seconds()),
    }


def _is_active(auth_session: AuthSession | None) -> bool:
    return (
        auth_session is not None
        and auth_session.revoked_at is None
        and as_utc(auth_session.expires_at) > utcnow()
    )


def rotate_refresh_token(
    session: Session, refresh_token: str
) -> tuple[AuthSession, str]:
    """Rotate a refresh token; presenting a rotated-out token revokes the session."""
    invalid = HTTPException(status_code=401, detail="Invalid refresh token")
    session_part, _, _ = refresh_token.partition(".")
    try:
        session_id = uuid.UUID(session_part)
    except ValueError as error:
        raise invalid from error
    auth_session = session.scalar(
        select(AuthSession).where(AuthSession.id == session_id).with_for_update()
    )
    if not _is_active(auth_session):
        raise invalid
    if not hmac.compare_digest(
        auth_session.refresh_token_hash, secret_hash(refresh_token)
    ):
        auth_session.revoked_at = utcnow()
        session.commit()
        raise invalid
    new_token = _new_refresh_token(auth_session.id)
    auth_session.refresh_token_hash = secret_hash(new_token)
    auth_session.last_used_at = utcnow()
    return auth_session, new_token


def revoke_sessions(
    session: Session, user_id: uuid.UUID, keep: uuid.UUID | None = None
) -> None:
    statement = update(AuthSession).where(
        AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
    )
    if keep is not None:
        statement = statement.where(AuthSession.id != keep)
    session.execute(statement.values(revoked_at=utcnow()))


def _login_identifier(email: str) -> str:
    return secret_hash(f"login:{email.strip().casefold()}")


def ensure_login_allowed(session: Session, email: str) -> None:
    window_start = utcnow() - LOGIN_FAILURE_WINDOW
    identifier = _login_identifier(email)
    failures, oldest = session.execute(
        select(func.count(AuthAttempt.id), func.min(AuthAttempt.created_at)).where(
            AuthAttempt.identifier == identifier,
            AuthAttempt.created_at >= window_start,
        )
    ).one()
    if failures >= login_failure_limit():
        retry_after = (as_utc(oldest) + LOGIN_FAILURE_WINDOW - utcnow()).total_seconds()
        raise HTTPException(
            status_code=429,
            detail="Too many failed sign-in attempts. Try again later.",
            headers={"Retry-After": str(max(1, int(retry_after) + 1))},
        )


def record_login_failure(session: Session, email: str) -> None:
    session.execute(
        delete(AuthAttempt).where(
            AuthAttempt.created_at < utcnow() - LOGIN_FAILURE_WINDOW
        )
    )
    session.add(AuthAttempt(identifier=_login_identifier(email), created_at=utcnow()))
    session.commit()


def clear_login_failures(session: Session, email: str) -> None:
    session.execute(
        delete(AuthAttempt).where(AuthAttempt.identifier == _login_identifier(email))
    )


def get_current_auth(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: Annotated[Session, Depends(get_session)],
) -> tuple[User, AuthSession]:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            _secret_key(),
            algorithms=[algorithm],
            options={"require": ["exp", "sub", "sid"]},
        )
        user_id = uuid.UUID(payload["sub"])
        session_id = uuid.UUID(payload["sid"])
        if payload.get("typ", "access") != "access":
            raise ValueError("not an access token")
    except (InvalidTokenError, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise unauthorized from error

    auth_session = session.get(AuthSession, session_id)
    if not _is_active(auth_session) or auth_session.user_id != user_id:
        raise unauthorized
    user = session.get(User, user_id)
    if user is None or user.password_hash is None:
        raise unauthorized
    return user, auth_session


def get_current_user(
    current: Annotated[tuple[User, AuthSession], Depends(get_current_auth)],
) -> User:
    return current[0]


def require_bootstrap_token(token: str | None) -> None:
    configured_token = os.environ.get("METIS_BOOTSTRAP_TOKEN", "")
    if not configured_token:
        raise HTTPException(status_code=503, detail="Organisation claiming is disabled")
    if token is None or not hmac.compare_digest(token, configured_token):
        raise HTTPException(status_code=403, detail="Invalid bootstrap token")


def require_membership(
    organisation_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[Session, Depends(get_session)],
) -> OrganisationMembership:
    membership = session.get(OrganisationMembership, (organisation_id, user.id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Organisation not found")
    return membership


def require_admin(
    membership: Annotated[OrganisationMembership, Depends(require_membership)],
) -> OrganisationMembership:
    if membership.role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organisation admin role required")
    return membership


def require_owner(
    membership: Annotated[OrganisationMembership, Depends(require_membership)],
) -> OrganisationMembership:
    if membership.role != "owner":
        raise HTTPException(status_code=403, detail="Organisation owner role required")
    return membership
