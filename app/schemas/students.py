"""Schemas for student intake — registration and the authenticated profile.

The response models here are the only place a student's own identity (name,
email) appears on the wire. Alumni payloads carry no names by construction, and
nothing in this module is ever returned for a *different* student: every route
that uses these resolves the subject from the bearer token, not from input.
"""

from __future__ import annotations

import re

from pydantic import Field, field_validator

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
    school_id: str = Field(description="Slug from GET /api/students/schools")
    email: str = Field(max_length=200)
    name: str | None = Field(default=None, max_length=160)
    year: StudentYear = StudentYear.freshman

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        # Normalized before the uniqueness check so casing can't mint duplicates.
        normalized = value.strip().lower()
        if not _EMAIL_RE.match(normalized):
            raise ValueError("not a valid email address")
        return normalized


class StudentCourseOut(CamelModel):
    code: str
    name: str
    semester_index: int


class StudentCourseIn(CamelModel):
    code: str = Field(max_length=32)
    name: str = Field(max_length=160)
    semester_index: int = Field(ge=0, le=7, description="0=Freshman Fall .. 7=Senior Spring")


class StudentOut(CamelModel):
    """The authenticated student's own profile."""

    id: str
    school_id: str | None
    school_name: str | None
    name: str | None
    email: str | None
    year: str
    major: str | None = Field(description="Primary declared major, from student_program")
    minors: list[str]
    intended_direction: str | None
    interests: list[str]
    courses: list[StudentCourseOut]


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
