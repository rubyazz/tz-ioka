"""Application settings loaded from environment / .env (pydantic-settings)."""

from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    # --- Application ---
    app_env: str = "development"  # development | production | test
    debug: bool = True
    log_level: str = "INFO"
    api_prefix: str = "/travel"
    app_name: str = "ioka travel API"
    app_version: str = "1.0.0"

    # --- PostgreSQL ---
    database_url: str = "postgresql+asyncpg://ioka:ioka@localhost:5432/ioka_travel"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    search_session_ttl_seconds: int = 900
    locations_cache_ttl_seconds: int = 3600
    search_deadline_seconds: int = 30

    # --- RabbitMQ ---
    rabbitmq_url: str = "amqp://ioka:ioka@localhost:5672/%2F"
    ticket_queue: str = "ticket.generation"
    ticket_dlq: str = "ticket.generation.dlq"
    ticket_fallback_sync: bool = True
    ticket_storage_dir: str = "/data/tickets"

    # --- Security / JWT ---
    jwt_secret: str = "dev-only-secret-change-me-0123456789abcdef0123456789"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # --- Avia content provider ---
    provider: str = "mock"  # mock | http
    provider_base_url: str | None = None
    provider_timeout_seconds: float = 8.0
    # Simulated provider latency (tests set both to 0)
    mock_latency_min_ms: int = 1500
    mock_latency_max_ms: int = 3000

    # --- Demo seed ---
    seed_demo_data: bool = True
    demo_agent_username: str = "agent"
    demo_agent_password: str = "agent123"
    demo_agent_balance: Decimal = Decimal("10000.00")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


_settings: Settings | None = None


def get_settings() -> Settings:
    """Cached settings accessor (single instance per process)."""
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


# Re-exported for convenient imports
__all__ = ["Settings", "get_settings"]
