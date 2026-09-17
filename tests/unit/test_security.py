"""Password hashing and JWT roundtrips."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.errors import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


async def test_password_hash_roundtrip() -> None:
    hashed = hash_password("s3cret-pass")
    assert hashed != "s3cret-pass"
    assert verify_password("s3cret-pass", hashed)
    assert not verify_password("wrong", hashed)


async def test_hash_is_salted() -> None:
    assert hash_password("same") != hash_password("same")


async def test_malformed_hash_rejected() -> None:
    assert not verify_password("x", "not-a-valid-hash")


async def test_token_roundtrip() -> None:
    agent_id = uuid.uuid4()
    token, expires_at = create_access_token(agent_id)
    payload = decode_access_token(token)
    assert payload["sub"] == str(agent_id)
    assert payload["scope"] == "agent"
    assert abs(expires_at - datetime.now(UTC)) < timedelta(minutes=61)


async def test_expired_token_rejected() -> None:
    settings = get_settings()
    now = datetime.now(UTC)
    stale = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
            "scope": "agent",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AuthenticationError):
        decode_access_token(stale)


async def test_garbage_token_rejected() -> None:
    with pytest.raises(AuthenticationError):
        decode_access_token("garbage.token.value")


async def test_wrong_scope_rejected() -> None:
    settings = get_settings()
    token = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
            "scope": "admin",
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(AuthenticationError):
        decode_access_token(token)
