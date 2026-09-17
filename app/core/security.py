"""Password hashing (argon2) and JWT issuing/verification."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings
from app.core.errors import AuthenticationError

_password_hasher = PasswordHasher()


def hash_password(raw: str) -> str:
    return _password_hasher.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _password_hasher.verify(hashed, raw)
    except VerifyMismatchError:
        return False
    except Exception:
        # malformed hash etc.
        return False


def create_access_token(agent_id: str | uuid.UUID) -> tuple[str, datetime]:
    """Return ``(token, expires_at)`` for the given agent."""
    settings = get_settings()
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.jwt_expire_minutes)
    payload: dict[str, Any] = {
        "sub": str(agent_id),
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": uuid.uuid4().hex,
        "scope": "agent",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires_at


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an access token; raises AuthenticationError."""
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Access token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Invalid access token") from exc
    if payload.get("scope") != "agent":
        raise AuthenticationError("Token does not grant agent access")
    return payload
