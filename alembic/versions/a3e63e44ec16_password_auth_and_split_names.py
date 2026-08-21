"""password auth and split names

Adds `students.password_hash`, `students.first_name` and `students.last_name`.

`password_hash` is nullable on purpose. Every student created before password
auth existed has none, and the login route treats a null hash as "cannot log
in" — an account without a password must be unable to authenticate, never able
to authenticate without one.

`first_name`/`last_name` are backfilled from the single `name` column with the
same split the view has been doing at read time: everything before the first
space, then the rest. That is lossy ("Mary Jane Watson" lands as Mary / Jane
Watson) and it is the best a one-column source supports. New registrations
write the two fields directly, so the guess only ever applies to rows that
predate this.

As in 41399da8d240, autogenerate proposed dropping the three GIN trigram
indexes. They are real, in use by search, and inexpressible in model metadata.
The drops are removed here — expect the same next time.

Revision ID: a3e63e44ec16
Revises: 41399da8d240
Create Date: 2026-08-21 04:41:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3e63e44ec16"
down_revision: str | None = "41399da8d240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("students", sa.Column("first_name", sa.String(length=80), nullable=True))
    op.add_column("students", sa.Column("last_name", sa.String(length=80), nullable=True))
    op.add_column("students", sa.Column("password_hash", sa.String(length=255), nullable=True))

    # split_part(name, ' ', 1) is the first token; substring past the first
    # space is the remainder, NULL when there is no space at all.
    op.execute(
        """
        UPDATE students
           SET first_name = NULLIF(split_part(btrim(name), ' ', 1), ''),
               last_name  = NULLIF(btrim(substring(btrim(name) from position(' ' in btrim(name)) + 1)), '')
         WHERE name IS NOT NULL
           AND btrim(name) <> ''
        """
    )
    # A single-word name has no surname; the substring above would otherwise
    # repeat the given name.
    op.execute(
        "UPDATE students SET last_name = NULL WHERE position(' ' in btrim(name)) = 0"
    )


def downgrade() -> None:
    op.drop_column("students", "password_hash")
    op.drop_column("students", "last_name")
    op.drop_column("students", "first_name")
