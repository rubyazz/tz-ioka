"""Auth API schemas."""

import uuid

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    """Agent credentials for POST /auth/agent/login."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class AgentBriefOut(BaseModel):
    """Public agent profile returned alongside the token."""

    id: uuid.UUID
    username: str
    company_name: str
    balance: str  # Decimal serialized as string — never as float
    currency: str


class LoginOut(BaseModel):
    """Login response: bearer token + authenticated agent snapshot."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until the token expires
    agent: AgentBriefOut
