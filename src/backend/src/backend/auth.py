import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash
from sqlalchemy.orm import Session

from backend.database import get_session
from backend.models import OrganisationMembership, User

password_hash = PasswordHash.recommended()
dummy_password_hash = password_hash.hash("invalid-login-placeholder-password")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")
algorithm = "HS256"
access_token_lifetime = timedelta(minutes=30)


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


def create_access_token(user_id: uuid.UUID) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + access_token_lifetime},
        _secret_key(),
        algorithm=algorithm,
    )


def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, _secret_key(), algorithms=[algorithm])
        user_id = uuid.UUID(payload["sub"])
    except (InvalidTokenError, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise unauthorized from error

    user = session.get(User, user_id)
    if user is None or user.password_hash is None:
        raise unauthorized
    return user


def require_bootstrap_token(token: str | None) -> None:
    import hmac

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
