"""Schemas for student intake — registration and the authenticated profile.

The response models here are the only place a student's own identity (name,
email) appears on the wire. Alumni payloads carry no names by construction, and
nothing in this module is ever returned for a *different* student: every route
that uses these resolves the subject from the bearer token, not from input.
"""

from __future__ import annotations

import re

from pydantic import Field, field_validator

from app.auth import MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH
from app.models import StudentYear
from app.schemas.constellation import CamelModel

# Structural sanity only — one @, a dotted domain, no spaces. Deliberately not a
# full RFC 5322 grammar and not proof the address exists: the address is an
# identifier here, and confirming it is the job of the invite/SSO flow that has
# to sit in front of registration before this runs anywhere real.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SchoolOut(CamelModel):
    id: str = Field(description="Slug — the value to send as schoolId at registration")
    name: str


class RegisterRequest(CamelModel):
    """Signup. `schoolId` is required and has no default.

    The frontend's form has no school field yet; `GET /api/students/schools`
    exists to populate one. Deriving the school from the email domain was the
    alternative and needs a domain-to-school mapping this service does not have
    — and a wrong guess is not a cosmetic error: the school is the tenant, and
    it is immutable through the API afterwards.
    """

    school_id: str = Field(description="Slug from GET /api/students/schools")
    email: str = Field(max_length=200)
    password: str = Field(
        min_length=MIN_PASSWORD_LENGTH,
        max_length=MAX_PASSWORD_LENGTH,
        description="Stored only as a scrypt hash",
    )
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    year: StudentYear = StudentYear.freshman

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        # Normalized before the uniqueness check so casing can't mint duplicates.
        normalized = value.strip().lower()
        if not _EMAIL_RE.match(normalized):
            raise ValueError("not a valid email address")
        return normalized

    @field_validator("password")
    @classmethod
    def _check_password(cls, value: str) -> str:
        # Length only. Composition rules ("one symbol, one digit") push people
        # towards shorter, more predictable passwords, and this is not the place
        # to relitigate that — a real deployment puts SSO in front of it anyway.
        if len(value.strip()) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        return value


class LoginRequest(CamelModel):
    email: str = Field(max_length=200)
    password: str = Field(max_length=MAX_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        # Normalized to match how registration stored it, so casing can't turn
        # one account into two. Deliberately *not* format-validated: login must
        # answer the same way for a malformed address as for an unknown one, and
        # a 422 here would say "no such account" out loud.
        return value.strip().lower()


def split_name(full_name: str | None) -> tuple[str | None, str | None]:
    """"Ada Lovelace" -> ("Ada", "Lovelace").

    An interim measure, and a lossy one: "Mary Jane Watson" gives a last name of
    "Jane Watson". The students table stores one `name` column because that is
    all registration ever collected, while the frontend's signup form collects
    the two separately — so the columns should land with that work, when there
    is finally something to put in them. Splitting here rather than storing a
    guess keeps the lossy step in the view, where it can be corrected.
    """
    if not full_name or not full_name.strip():
        return None, None
    first, _, rest = full_name.strip().partition(" ")
    return first, rest.strip() or None


class StudentCourseOut(CamelModel):
    """`id` is the course code, which is what `PUT /me/courses` takes as `code`.

    One identifier rather than two names for the same value: the code is already
    unique per student (the table has a unique index on it), and the frontend
    keys its list on `id`. Read a transcript, edit it, send it back — `id` maps
    straight to `code`.
    """

    id: str = Field(description="The course code — round-trips as `code` on write")
    name: str
    semester: str = Field(description='Derived label, e.g. "Sophomore Spring"')
    # `semesterIndex` stays the source of truth for timing; the label above is
    # derived from it. Keeping both means the client never has to parse a label
    # back into an ordering.
    semester_index: int


class StudentCourseIn(CamelModel):
    code: str = Field(max_length=32)
    name: str = Field(max_length=160)
    semester_index: int = Field(ge=0, le=7, description="0=Freshman Fall .. 7=Senior Spring")


class StudentOut(CamelModel):
    """The authenticated student's own profile."""

    id: str
    # The slug stays alongside the display name: it is the tenant identifier
    # every alumni read is scoped by, and the one value support needs when a
    # constellation looks wrong.
    school_id: str | None
    school: str | None = Field(description="Display name of the student's school")
    first_name: str | None
    last_name: str | None
    email: str | None
    current_year: str
    declared_major: str | None = Field(
        description="Primary declared major, from student_program"
    )
    minors: list[str]
    intended_direction: str | None
    interests: list[str]
    courses_completed: list[StudentCourseOut]


class RegisterResponse(CamelModel):
    """Registration is the one and only time the token is transmitted.

    Only its SHA-256 hash is stored, so a lost token cannot be recovered — it can
    only be reissued, which is a deliberate property rather than a gap.
    """

    token: str = Field(description="Bearer token — shown once, store it client-side")
    student: StudentOut


class ProfileUpdate(CamelModel):
    """A partial update — omitted fields keep their value, explicit `null` clears.

    `schoolId` is deliberately absent: a student cannot move themselves between
    tenants, since that would hand them another school's alumni corpus.
    """

    year: StudentYear | None = None
    major: str | None = Field(default=None, max_length=128)
    intended_direction: str | None = Field(default=None, max_length=128)
    interests: list[str] | None = None


class CoursesUpdate(CamelModel):
    courses: list[StudentCourseIn] = Field(
        description="The full transcript — this replaces what's stored, it does not merge"
    )


class ActivityOut(CamelModel):
    """One line in the Dashboard's recent-activity feed.

    `label` is served as stored, not reassembled. The feed records what the
    student did at the time — if a cluster is renamed or a saved path deleted,
    the entry should still read the way it read then.
    """

    id: int
    kind: str = Field(description="'explored' | 'saved_path' | 'simulated' | …")
    label: str
    at: str = Field(description="ISO-8601 timestamp")
