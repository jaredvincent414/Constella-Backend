"""saved_paths for the Create Path feature

A student's bookmarks of alumni journeys; selecting several feeds the combine
endpoint.

Revision ID: c7e2d4a9b010
Revises: f3c9a1b27e10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e2d4a9b010"
down_revision: str | None = "f3c9a1b27e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "saved_paths",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("student_id", sa.String(length=64), nullable=False),
        sa.Column("alumnus_id", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["alumnus_id"], ["alumni.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_saved_paths_student_id"), "saved_paths", ["student_id"])
    op.create_index(op.f("ix_saved_paths_alumnus_id"), "saved_paths", ["alumnus_id"])
    op.create_index(
        "ix_saved_paths_student_alumnus", "saved_paths", ["student_id", "alumnus_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_saved_paths_student_alumnus", table_name="saved_paths")
    op.drop_index(op.f("ix_saved_paths_alumnus_id"), table_name="saved_paths")
    op.drop_index(op.f("ix_saved_paths_student_id"), table_name="saved_paths")
    op.drop_table("saved_paths")
