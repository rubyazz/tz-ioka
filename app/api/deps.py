"""FastAPI dependencies: auth (JWT Bearer), DB session, settings."""

import uuid
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.logging import agent_id_var, bind_request_context
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models import Agent
from app.repositories.agent import AgentRepository

_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Paste the access_token returned by POST /travel/auth/agent/login",
)


async def get_current_agent(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Agent:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing Bearer token")
    payload = decode_access_token(credentials.credentials)
    try:
        agent_id = uuid.UUID(str(payload["sub"]))
    except ValueError as exc:
        raise AuthenticationError("Invalid token subject") from exc

    agent = await AgentRepository(session).get_by_id(agent_id)
    if agent is None:
        raise AuthenticationError("Agent no longer exists")
    if not agent.is_active:
        raise AuthorizationError("Agent account is disabled")

    bind_request_context(agent_id=str(agent.id))
    agent_id_var.set(str(agent.id))
    return agent


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
CurrentAgent = Annotated[Agent, Depends(get_current_agent)]
