"""Create the demo agent if it does not exist yet.

Run manually:  python -m scripts.seed
(also invoked automatically on API startup when SEED_DEMO_DATA=true)
"""

import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.db.session import get_session_factory
from app.models import Agent

DEMO_COMPANY = "Demo Travel Agency LLC"
log = get_logger("seed")


async def seed_demo_agent() -> None:
    settings = get_settings()
    async with get_session_factory()() as session:
        existing = (
            await session.execute(
                select(Agent).where(Agent.username == settings.demo_agent_username)
            )
        ).scalar_one_or_none()
        if existing is not None:
            log.info("demo_agent_exists", username=existing.username)
            return
        session.add(
            Agent(
                username=settings.demo_agent_username,
                email=f"{settings.demo_agent_username}@demo.ioka.uz",
                password_hash=hash_password(settings.demo_agent_password),
                company_name=DEMO_COMPANY,
                balance=settings.demo_agent_balance,
            )
        )
        await session.commit()
    log.info(
        "demo_agent_created",
        username=settings.demo_agent_username,
        balance=str(settings.demo_agent_balance),
    )


if __name__ == "__main__":
    configure_logging("INFO")
    asyncio.run(seed_demo_agent())
