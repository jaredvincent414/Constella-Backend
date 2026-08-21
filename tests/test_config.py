"""Settings that a deployment depends on getting right.

These exist because the failure they prevent is a container that crashes on
boot with a message pointing at the wrong problem.
"""

from __future__ import annotations

import pytest

from app.config import Settings


class TestDatabaseUrlDriver:
    """Managed Postgres providers hand out a URL this app cannot use directly.

    Render, Heroku and most others emit `postgres://`, which selects
    SQLAlchemy's synchronous psycopg driver — not installed here. The result is
    a ModuleNotFoundError at engine construction.
    """

    @pytest.mark.parametrize(
        "given",
        [
            "postgres://user:pass@host:5432/db",
            "postgresql://user:pass@host:5432/db",
        ],
    )
    def test_sync_schemes_are_rewritten(self, given):
        assert Settings(database_url=given).database_url == (
            "postgresql+asyncpg://user:pass@host:5432/db"
        )

    def test_an_async_url_is_left_alone(self):
        url = "postgresql+asyncpg://user:pass@host:5432/db"
        assert Settings(database_url=url).database_url == url

    def test_credentials_and_query_string_survive(self):
        given = "postgres://u:p%40ss@host:5432/db?sslmode=require"
        assert Settings(database_url=given).database_url == (
            "postgresql+asyncpg://u:p%40ss@host:5432/db?sslmode=require"
        )

    def test_a_non_postgres_url_is_untouched(self):
        """The rewrite is specific to the one driver this app can use — it must
        not mangle a URL naming some other backend."""
        assert Settings(database_url="sqlite+aiosqlite:///x.db").database_url == (
            "sqlite+aiosqlite:///x.db"
        )
