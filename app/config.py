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

    # --- Major/minor matching --------------------------------------------
    # How much a shared minor counts relative to a shared major in the 20%
    # major-match component. A shared minor should nudge the score, not swing it,
    # so this is well below 1.0. Never hardcoded at the call site.
    minor_match_weight: float = 0.35

    # --- Derived minors (placeholder data only) --------------------------
    # A concentration of coursework outside a student's declared field becomes a
    # de facto (inferred) minor. Either threshold qualifies.
    derived_minor_min_courses: int = 4
    derived_minor_min_credits: float = 12.0

    # --- Create Path (path combining) ------------------------------------
    # Courses kept per year-stage in a combined path. The frontend's plan view
    # shows a handful per semester; ranking by cross-path frequency picks them.
    combine_max_courses_per_stage: int = 4

    # Comma-separated rather than a list field: pydantic-settings tries to
    # JSON-decode complex types from env vars, which makes plain CSV fail.
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    log_level: str = "INFO"

    # --- Auth ------------------------------------------------------------
    # Shared key gating /api/admin. Empty (the default) means admin endpoints
    # are DISABLED rather than open — set ADMIN_API_KEY to enable them.
    admin_api_key: str = ""

    # --- Data ingestion --------------------------------------------------
    # Which source adapter feeds the corpus. "midfield" is the practice dataset;
    # swap this for a real registrar adapter once school data arrives (see
    # app/ingest/README.md). The rest of the app never learns which source ran —
    # it only reads the ORM tables the loader populated.
    data_source: str = "midfield"
    # Directory holding the source's files. For MIDFIELD this is the folder with
    # student/term/course/degree in .parquet or .csv form.
    data_path: str = "/Users/vinnie/Desktop/midfielddata/export"
    # 0 = load every alumnus. A cap keeps a validation load fast; the full
    # MIDFIELD corpus is ~50k degree-earners.
    ingest_alumni_limit: int = 1500
    # Synthetic "current students" derived from real pre-pivot transcripts, so
    # the matching pipeline has something to score against out of the box. Real
    # current-student profiles come from the app, not from this dataset.
    ingest_student_count: int = 30
    ingest_seed: int = 42

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
