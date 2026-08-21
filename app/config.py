"""Application settings, loaded from the environment (see .env.example)."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://constella:constella@localhost:5433/constella"
    redis_url: str = "redis://localhost:6380/0"

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """Accept the URL managed Postgres providers actually hand out.

        Render, Heroku and most others emit `postgres://` (and some
        `postgresql://`), both of which select SQLAlchemy's *synchronous* psycopg
        driver — which this application does not install. The failure is a
        `ModuleNotFoundError` at engine construction, i.e. a container that
        crashes on boot with a message pointing at the wrong problem.

        Rewriting the scheme here is safe because there is exactly one driver
        this app can use. A URL that already names a different async driver is
        left alone.
        """
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value[len(prefix):]
        return value

    # Must outlive the interval between precompute runs. A TTL shorter than the
    # job period is the worst of both worlds: entries expire long before the job
    # refreshes them, so the cache is cold for most of the day and nearly every
    # request pays a full inline recompute. 30h leaves headroom over a daily job
    # — raise it if the job runs less often than that.
    # --- Database pool -----------------------------------------------------
    # `pool_pre_ping` issues a SELECT 1 before handing out any connection, which
    # measured at ~0.7ms on *every* request that touches Postgres. `pool_recycle`
    # covers the common case it guards — a connection idled out by the server —
    # without a per-request round trip. Turn pre-ping back on behind a proxy or
    # a failover pair, where connections can drop unannounced mid-pool.
    db_pool_pre_ping: bool = False
    db_pool_recycle_seconds: int = 1800
    # SQLAlchemy's defaults are 5 + 10. An inline cache miss can hold a
    # connection for the length of a corpus load, so the pool has to be wider
    # than the number of requests you expect to be missing at once.
    db_pool_size: int = 20
    db_max_overflow: int = 10

    cache_ttl_seconds: int = 108_000

    # How long a resolved bearer token stays cached. This is the window in which
    # a deleted student could still authenticate, so it is deliberately short —
    # short enough that it never substitutes for the revocation endpoint the
    # README still lists as missing. 0 disables the cache entirely.
    auth_cache_ttl_seconds: int = 60

    # --- Login throttling -------------------------------------------------
    # Counted per email and per client address; whichever trips first blocks.
    # These are counters with a TTL, not a lockout flag, so a blocked identity
    # frees itself — an attacker failing on someone's behalf costs them a wait,
    # not their account.
    login_max_failures: int = 10
    login_failure_window_seconds: int = 900

    # Single-flight. On a miss, one caller computes and the rest wait for it
    # rather than all recomputing the same payload. The lock TTL bounds how long
    # a crashed holder can block others; the wait bounds how long a follower
    # waits before giving up and computing anyway. Neither can fail a request.
    cache_lock_ttl_seconds: int = 30
    cache_lock_wait_seconds: float = 2.0

    # Ceiling on how many query variants the nightly job re-warms per student.
    # The list comes from what students actually requested, which makes it
    # user-controlled growth — a cap is what keeps one heavy explorer from
    # stretching the whole job.
    precompute_max_queries_per_student: int = 8

    # How close a free-text facet has to be to count as a match. Same scale and
    # the same kernel as the scorer's pivot-query threshold, so "close enough"
    # means one thing in this codebase rather than two.
    facet_match_threshold: float = 0.3

    # Collapse a burst of identical activity entries. Toggling a filter three
    # times should leave one line in a four-item feed, not three; coming back
    # tomorrow should still leave a new one.
    activity_dedupe_seconds: int = 900
    activity_feed_limit: int = 20

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
