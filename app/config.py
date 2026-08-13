"""Application settings, loaded from the environment (see .env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://constella:constella@localhost:5433/constella"
    redis_url: str = "redis://localhost:6380/0"

    cache_ttl_seconds: int = 3600

    constellation_min_alumni: int = 50
    constellation_max_alumni: int = 200
    simulator_top_n: int = 5

    # Kept in sync with the frontend's edge render rules. The backend applies the
    # same cap so it isn't shipping edges the frontend would discard.
    edge_min_weight: float = 0.25
    edge_max_count: int = 12

    # Comma-separated rather than a list field: pydantic-settings tries to
    # JSON-decode complex types from env vars, which makes plain CSV fail.
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    log_level: str = "INFO"

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
