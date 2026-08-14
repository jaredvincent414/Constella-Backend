"""career_outcomes — employment data, the clustering axis

Adds the outcome table the constellation clusters on, plus a 'synthetic'
provenance value for seeded (placeholder) employment.

Revision ID: d5b8f1c3a920
Revises: c7e2d4a9b010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5b8f1c3a920"
down_revision: str | None = "c7e2d4a9b010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROV = postgresql.ENUM(
    "reported", "derived", "synthetic", name="provenance", create_type=False
)


def upgrade() -> None:
    # ADD VALUE can't run inside the migration's transaction and then be used in
    # it, so commit it on its own first.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE provenance ADD VALUE IF NOT EXISTS 'synthetic'")

    op.create_table(
        "career_outcomes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alumnus_id", sa.String(length=64), nullable=False),
        sa.Column("industry", sa.String(length=128), nullable=False),
        sa.Column("occupation", sa.String(length=128), nullable=False),
        sa.Column("employer_region", sa.String(length=64), nullable=True),
        sa.Column("years_post_grad", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provenance", _PROV, nullable=False, server_default="synthetic"),
        sa.ForeignKeyConstraint(["alumnus_id"], ["alumni.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_career_outcomes_alumnus_id"), "career_outcomes", ["alumnus_id"])
    op.create_index(op.f("ix_career_outcomes_industry"), "career_outcomes", ["industry"])


def downgrade() -> None:
    op.drop_index(op.f("ix_career_outcomes_industry"), table_name="career_outcomes")
    op.drop_index(op.f("ix_career_outcomes_alumnus_id"), table_name="career_outcomes")
    op.drop_table("career_outcomes")
    # Postgres has no DROP VALUE; the 'synthetic' enum member is left in place
    # (harmless — nothing references it once the table is gone).
