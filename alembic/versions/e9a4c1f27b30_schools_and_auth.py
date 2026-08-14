"""schools (multi-tenancy) + student auth fields

Adds the school tenant, student identity/token columns, and school_id on both
students and alumni. Backfills schools from the alumni `outcome_org`
(institution), points alumni at them, and points each student at their source
alumnus's school (the synthetic `mid-student-*` ids) or a fallback demo school.

Revision ID: e9a4c1f27b30
Revises: d5b8f1c3a920
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9a4c1f27b30"
down_revision: str | None = "d5b8f1c3a920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schools",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column("students", sa.Column("school_id", sa.String(length=64), nullable=True))
    op.add_column("students", sa.Column("name", sa.String(length=160), nullable=True))
    op.add_column("students", sa.Column("email", sa.String(length=200), nullable=True))
    op.add_column("students", sa.Column("auth_token_hash", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_students_school_id"), "students", ["school_id"])
    op.create_unique_constraint("uq_students_email", "students", ["email"])
    op.create_index(op.f("ix_students_auth_token_hash"), "students", ["auth_token_hash"],
                    unique=True)
    op.create_foreign_key(
        "fk_students_school", "students", "schools", ["school_id"], ["id"], ondelete="CASCADE"
    )

    op.add_column("alumni", sa.Column("school_id", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_alumni_school_id"), "alumni", ["school_id"])
    op.create_foreign_key(
        "fk_alumni_school", "alumni", "schools", ["school_id"], ["id"], ondelete="CASCADE"
    )

    # --- backfill --------------------------------------------------------
    # A school per distinct institution (alumni.outcome_org), slugified.
    op.execute(
        """
        INSERT INTO schools (id, name)
        SELECT DISTINCT
               regexp_replace(lower(outcome_org), '[^a-z0-9]+', '-', 'g'),
               outcome_org
        FROM alumni
        WHERE outcome_org IS NOT NULL AND outcome_org <> ''
        ON CONFLICT (id) DO NOTHING
        """
    )
    # A fallback school for any orphans (e.g. seed demo data).
    op.execute(
        "INSERT INTO schools (id, name) VALUES ('demo-university', 'Demo University') "
        "ON CONFLICT (id) DO NOTHING"
    )
    op.execute(
        """
        UPDATE alumni
        SET school_id = regexp_replace(lower(outcome_org), '[^a-z0-9]+', '-', 'g')
        WHERE outcome_org IS NOT NULL AND outcome_org <> ''
        """
    )
    op.execute("UPDATE alumni SET school_id = 'demo-university' WHERE school_id IS NULL")

    # Synthetic students are 'mid-student-<alumnus_id>' — inherit that alumnus's
    # school so the demo student can actually see their source cohort.
    op.execute(
        """
        UPDATE students s
        SET school_id = a.school_id
        FROM alumni a
        WHERE s.school_id IS NULL
          AND a.id = regexp_replace(s.id, '^mid-student-', '')
        """
    )
    op.execute("UPDATE students SET school_id = 'demo-university' WHERE school_id IS NULL")


def downgrade() -> None:
    op.drop_constraint("fk_alumni_school", "alumni", type_="foreignkey")
    op.drop_index(op.f("ix_alumni_school_id"), table_name="alumni")
    op.drop_column("alumni", "school_id")

    op.drop_constraint("fk_students_school", "students", type_="foreignkey")
    op.drop_index(op.f("ix_students_auth_token_hash"), table_name="students")
    op.drop_constraint("uq_students_email", "students", type_="unique")
    op.drop_index(op.f("ix_students_school_id"), table_name="students")
    op.drop_column("students", "auth_token_hash")
    op.drop_column("students", "email")
    op.drop_column("students", "name")
    op.drop_column("students", "school_id")

    op.drop_table("schools")
