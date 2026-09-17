"""Agent authentication: credential check + JWT issuing."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.core.security import create_access_token, verify_password
from app.models import Agent
from app.repositories.agent import AgentRepository

log = get_logger(__name__)


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def login(self, username: str, password: str) -> tuple[Agent, str, datetime]:
        """Verify credentials and return ``(agent, token, expires_at)``.

        Unknown agent, wrong password and disabled account all produce the
        same uniform AuthenticationError (no user enumeration). The password
        is never logged.
        """
        agent = await AgentRepository(self._session).get_by_username(username)
        if (
            agent is None
            or not verify_password(password, agent.password_hash)
            or not agent.is_active
        ):
            log.warning("login_failed", username=username)
            raise AuthenticationError("Invalid username or password")
        token, expires_at = create_access_token(agent.id)
        log.info("agent_logged_in", agent_id=str(agent.id), username=username)
        return agent, token, expires_at
