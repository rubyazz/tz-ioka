"""Agent authentication endpoints."""

from datetime import UTC, datetime

from fastapi import APIRouter

from app.api.deps import SessionDep, SettingsDep
from app.schemas.auth import AgentBriefOut, LoginIn, LoginOut
from app.services.auth import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/agent/login", response_model=LoginOut)
async def agent_login(body: LoginIn, session: SessionDep, settings: SettingsDep) -> LoginOut:
    """Exchange username/password for a bearer JWT (no auth required)."""
    agent, token, expires_at = await AuthService(session, settings).login(
        body.username, body.password
    )
    return LoginOut(
        access_token=token,
        token_type="bearer",
        expires_in=max(0, int((expires_at - datetime.now(UTC)).total_seconds())),
        agent=AgentBriefOut(
            id=agent.id,
            username=agent.username,
            company_name=agent.company_name,
            balance=f"{agent.balance:.2f}",
            currency=agent.currency,
        ),
    )
