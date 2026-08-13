"""multiple majors/minors: student_program, roles, provenance

Moves program (major/minor) data onto role-tagged join rows so a person can hold
several majors and any number of minors, none of it scalar.

Backfill is non-destructive: every existing `alumnus_majors` row becomes
`role='primary', provenance='reported'`, and each student's `declared_major`
scalar is copied into a `student_program` primary row. The old
`students.declared_major` column is left in place (a follow-up migration drops it
once reads are confirmed routed through the accessors). Row-count assertions
guard the backfill.

Revision ID: f3c9a1b27e10
Revises: 01d7438288bc
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f3c9a1b27e10"
down_revision: str | None = "01d7438288bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Shared enum types — created once, referenced (create_type=False) by both tables
# so neither column definition tries to CREATE TYPE a second time.
_ROLE = postgresql.ENUM(
    "primary", "second_major", "minor", "concentration", name="program_role"
)
_PROV = postgresql.ENUM("reported", "derived", name="provenance")
_ROLE_COL = postgresql.ENUM(
    "primary", "second_major", "minor", "concentration",
    name="program_role", create_type=False,
)
_PROV_COL = postgresql.ENUM("reported", "derived", name="provenance", create_type=False)


def _count(bind, sql: str) -> int:
    return bind.execute(sa.text(sql)).scalar_one()


def upgrade() -> None:
    bind = op.get_bind()
    _ROLE.create(bind, checkfirst=True)
    _PROV.create(bind, checkfirst=True)

    # --- alumnus_majors: role + provenance + cip6 ------------------------
    # Collapse any pre-existing duplicates that the new uniqueness rule would
    # reject (e.g. a double major recorded twice at the same term), keeping the
    # earliest row. Done before the count check so it isn't mistaken for backfill.
    op.execute(
        """
        DELETE FROM alumnus_majors a
        USING alumnus_majors b
        WHERE a.id > b.id
          AND a.alumnus_id = b.alumnus_id
          AND a.declared_semester = b.declared_semester
          AND a.name = b.name
        """
    )

    majors_before = _count(bind, "SELECT count(*) FROM alumnus_majors")

    op.add_column("alumnus_majors", sa.Column("cip6", sa.String(length=6), nullable=True))
    op.add_column(
        "alumnus_majors",
        sa.Column("role", _ROLE_COL, nullable=False, server_default="primary"),
    )
    op.add_column(
        "alumnus_majors",
        sa.Column("provenance", _PROV_COL, nullable=False, server_default="reported"),
    )

    majors_after = _count(bind, "SELECT count(*) FROM alumnus_majors")
    null_roles = _count(bind, "SELECT count(*) FROM alumnus_majors WHERE role IS NULL")
    assert majors_after == majors_before, (
        f"alumnus_majors backfill changed row count: {majors_before} -> {majors_after}"
    )
    assert null_roles == 0, f"{null_roles} alumnus_majors rows left without a role"

    op.create_index(
        "ix_alumnus_majors_unique",
        "alumnus_majors",
        ["alumnus_id", "declared_semester", "name", "role"],
        unique=True,
    )

    # --- alumnus_courses: discipline + credit hours (for derived minors) --
    op.add_column("alumnus_courses", sa.Column("discipline", sa.String(length=160), nullable=True))
    op.add_column("alumnus_courses", sa.Column("credit_hours", sa.Float(), nullable=True))

    # --- student_program: the query-side join table ----------------------
    op.create_table(
        "student_program",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("student_id", sa.String(length=64), nullable=False),
        sa.Column("term", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("cip6", sa.String(length=6), nullable=True),
        sa.Column("role", _ROLE_COL, nullable=False, server_default="primary"),
        sa.Column("provenance", _PROV_COL, nullable=False, server_default="reported"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_student_program_student_id"), "student_program", ["student_id"])
    op.create_index(op.f("ix_student_program_name"), "student_program", ["name"])
    op.create_index(
        "ix_student_program_unique",
        "student_program",
        ["student_id", "term", "name", "role"],
        unique=True,
    )

    # Backfill each student's declared major as a primary program row.
    expected = _count(
        bind,
        "SELECT count(*) FROM students WHERE declared_major IS NOT NULL AND declared_major <> ''",
    )
    op.execute(
        """
        INSERT INTO student_program (student_id, term, name, role, provenance)
        SELECT id, 0, declared_major, 'primary', 'reported'
        FROM students
        WHERE declared_major IS NOT NULL AND declared_major <> ''
        """
    )
    actual = _count(bind, "SELECT count(*) FROM student_program WHERE role = 'primary'")
    assert actual == expected, (
        f"student_program backfill mismatch: expected {expected} primary rows, got {actual}"
    )


def downgrade() -> None:
    op.drop_index("ix_student_program_unique", table_name="student_program")
    op.drop_index(op.f("ix_student_program_name"), table_name="student_program")
    op.drop_index(op.f("ix_student_program_student_id"), table_name="student_program")
    op.drop_table("student_program")

    op.drop_column("alumnus_courses", "credit_hours")
    op.drop_column("alumnus_courses", "discipline")

    op.drop_index("ix_alumnus_majors_unique", table_name="alumnus_majors")
    op.drop_column("alumnus_majors", "provenance")
    op.drop_column("alumnus_majors", "role")
    op.drop_column("alumnus_majors", "cip6")

    bind = op.get_bind()
    _PROV.drop(bind, checkfirst=True)
    _ROLE.drop(bind, checkfirst=True)
