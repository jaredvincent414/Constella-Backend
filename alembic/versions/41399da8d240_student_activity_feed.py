"""student activity feed

Adds `student_activity`, the Dashboard's "Recent activity" list.

Autogenerate also wanted to drop `ix_alumni_career_area_trgm`,
`ix_alumnus_courses_code_trgm` and `ix_alumnus_majors_name_trgm`. Those are
real, in use, and deliberately not declared on the models — they are GIN
trigram indexes created by an earlier migration, and SQLAlchemy's model
metadata has no way to express them, so every autogenerate run will propose
removing them. The drops are removed here. Check for them again the next time
you autogenerate.

Revision ID: 41399da8d240
Revises: e9a4c1f27b30
Create Date: 2026-08-21 04:02:18.600978
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "41399da8d240"
down_revision: str | None = "e9a4c1f27b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "student_activity",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("student_id", sa.String(length=64), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "explored",
                "saved_path",
                "removed_path",
                "combined_paths",
                "simulated",
                "updated_profile",
                "updated_courses",
                name="activity_kind",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # CASCADE: the feed is a record of one student's own actions and has no
        # meaning once the account is gone.
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_student_activity_created_at"), "student_activity", ["created_at"], unique=False
    )
    # The only query this table serves: one student's newest entries.
    op.create_index(
        "ix_student_activity_recent", "student_activity", ["student_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_student_activity_recent", table_name="student_activity")
    op.drop_index(op.f("ix_student_activity_created_at"), table_name="student_activity")
    op.drop_table("student_activity")
