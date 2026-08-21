"""Integration tests for the auth and multi-tenancy boundary.

These need a migrated Postgres (`docker compose up -d` + `alembic upgrade head`)
and skip cleanly without one, so the suite still runs on a bare checkout.

Everything created here is namespaced `test-sec-*` and torn down afterwards, so
running the suite against a development database doesn't disturb its contents.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth import hash_token
from app.config import settings
from app.db import SessionLocal
from app.jobs.recompute import load_corpus, warm_student
from app.main import app
from app.matching import build_corpus
from app.models import (
    Alumnus,
    AlumnusCourse,
    AlumnusMajor,
    CareerOutcome,
    School,
    Student,
    StudentCourse,
    StudentProgram,
    StudentYear,
)

SCHOOL_A = "test-sec-school-a"
SCHOOL_B = "test-sec-school-b"
ALUM_A = "test-sec-alum-a"
ALUM_B = "test-sec-alum-b"
STUDENT_A = "test-sec-stu-a"
STUDENT_B = "test-sec-stu-b"
TOKEN_A = "test-sec-token-a"
TOKEN_B = "test-sec-token-b"


def _db_ready() -> bool:
    """True when Postgres is up *and* the schools migration has been applied.

    Uses a throwaway engine so the probe's event loop never touches the pool the
    tests themselves run on — asyncpg connections are bound to their loop.
    """

    async def probe() -> bool:
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("select 1 from schools limit 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    try:
        return asyncio.run(probe())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_ready(),
    reason="needs a migrated Postgres (docker compose up -d && alembic upgrade head)",
)


def _alumnus(alumnus_id: str, school_id: str, career_area: str) -> Alumnus:
    alumnus = Alumnus(
        id=alumnus_id,
        school_id=school_id,
        graduation_year=2022,
        outcome_title="Health Policy Analyst",
        outcome_org="State Health Dept",
        career_area=career_area,
        interests=["Global Health Club"],
    )
    alumnus.courses = [
        AlumnusCourse(course_code="BIO 101", course_name="Bio 101", semester_index=0),
        AlumnusCourse(course_code="PH 201", course_name="Intro to Public Health", semester_index=3),
    ]
    alumnus.majors = [AlumnusMajor(name="Biochemistry", declared_semester=1, is_final=True)]
    alumnus.outcomes = [
        CareerOutcome(industry=career_area, occupation="Analyst", years_post_grad=1)
    ]
    return alumnus


async def _cleanup() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Student).where(Student.id.in_([STUDENT_A, STUDENT_B])))
        await session.execute(delete(Alumnus).where(Alumnus.id.in_([ALUM_A, ALUM_B])))
        # Any student registered through the API during a test.
        await session.execute(delete(Student).where(Student.school_id.in_([SCHOOL_A, SCHOOL_B])))
        await session.execute(delete(School).where(School.id.in_([SCHOOL_A, SCHOOL_B])))
        await session.commit()


@pytest.fixture
async def seeded():
    """Two schools, one alumnus and one student each."""
    await _cleanup()
    async with SessionLocal() as session:
        session.add_all(
            [
                School(id=SCHOOL_A, name="Test Security School A"),
                School(id=SCHOOL_B, name="Test Security School B"),
            ]
        )
        await session.commit()

        session.add_all(
            [
                _alumnus(ALUM_A, SCHOOL_A, "Health Policy"),
                _alumnus(ALUM_B, SCHOOL_B, "Health Policy"),
            ]
        )
        student_a = Student(
            id=STUDENT_A,
            school_id=SCHOOL_A,
            name="Ada",
            email="ada@test-sec.example.edu",
            auth_token_hash=hash_token(TOKEN_A),
            year=StudentYear.sophomore,
            intended_direction="Health Policy",
            interests=["Global Health Club"],
        )
        student_a.courses = [
            StudentCourse(course_code="BIO 101", course_name="Bio 101", semester_index=0)
        ]
        student_b = Student(
            id=STUDENT_B,
            school_id=SCHOOL_B,
            name="Blair",
            email="blair@test-sec.example.edu",
            auth_token_hash=hash_token(TOKEN_B),
            year=StudentYear.sophomore,
            interests=[],
        )
        session.add_all([student_a, student_b])
        await session.commit()

    yield
    await _cleanup()


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------
# 401 — no token, no access
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/constellation", None),
        ("get", f"/api/alumni/{ALUM_A}/timeline", None),
        ("post", "/api/simulate", {"toMajor": "Health Policy"}),
        ("get", "/api/paths", None),
        ("post", "/api/paths", {"alumnusId": ALUM_A}),
        ("post", "/api/paths/combine", {"pathIds": [1, 2]}),
        ("get", "/api/students/me", None),
        ("put", "/api/students/me", {"year": "junior"}),
        ("put", "/api/students/me/courses", {"courses": []}),
    ],
)
async def test_student_routes_require_a_token(client, seeded, method, path, body):
    response = await getattr(client, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 401


async def test_invalid_token_is_rejected(client, seeded):
    response = await client.get("/api/students/me", headers=auth("not-a-real-token"))
    assert response.status_code == 401


async def test_malformed_authorization_header_is_rejected(client, seeded):
    response = await client.get("/api/students/me", headers={"Authorization": TOKEN_A})
    assert response.status_code == 401


async def test_token_hash_is_not_accepted_as_a_token(client, seeded):
    """Reading the stored hash out of the database must not yield a usable
    credential — otherwise a read-only DB leak would be a full impersonation."""
    response = await client.get("/api/students/me", headers=auth(hash_token(TOKEN_A)))
    assert response.status_code == 401


# --------------------------------------------------------------------------
# Tenant isolation
# --------------------------------------------------------------------------


async def test_school_less_student_is_refused(client, seeded):
    """A null tenant must fail closed. `list_alumni(school_id=None)` means
    *unscoped* — every school's corpus — so an account in this state has to be
    rejected at the door rather than served an over-broad constellation."""
    async with SessionLocal() as session:
        orphan = Student(
            id="test-sec-orphan",
            school_id=None,
            email="orphan@test-sec.example.edu",
            auth_token_hash=hash_token("test-sec-orphan-token"),
            year=StudentYear.freshman,
            interests=[],
        )
        session.add(orphan)
        await session.commit()

    try:
        response = await client.get(
            "/api/constellation", headers=auth("test-sec-orphan-token")
        )
        assert response.status_code == 403
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Student).where(Student.id == "test-sec-orphan"))
            await session.commit()


async def test_timeline_is_readable_within_your_school(client, seeded):
    response = await client.get(f"/api/alumni/{ALUM_A}/timeline", headers=auth(TOKEN_A))
    assert response.status_code == 200


async def test_timeline_404s_across_schools(client, seeded):
    """Student A asking for School B's alumnus gets the same answer as for an id
    that doesn't exist — existence itself must not leak."""
    cross = await client.get(f"/api/alumni/{ALUM_B}/timeline", headers=auth(TOKEN_A))
    missing = await client.get("/api/alumni/no-such-alumnus/timeline", headers=auth(TOKEN_A))
    assert cross.status_code == 404
    assert missing.status_code == 404


async def test_timeline_cache_is_not_shared_between_students(client, seeded):
    """Two students at the same school must not see each other's comparison.

    The detail payload is scored against the *caller's* transcript —
    `sharedCourses` is rendered with their own course names. The timeline cache
    key therefore carries the student id; keyed on the alumnus alone, the second
    reader here would be served Ada's shared-course list as if it were theirs.
    """
    registered = await client.post(
        "/api/students/register",
        json={
            "schoolId": SCHOOL_A,
            "email": "no-courses@test-sec.example.edu",
            "name": "No Courses",
            "year": "sophomore",
        },
    )
    assert registered.status_code == 201
    other = registered.json()["token"]

    # Ada first, so the entry is warm before the second student asks.
    ada = await client.get(f"/api/alumni/{ALUM_A}/timeline", headers=auth(TOKEN_A))
    assert ada.status_code == 200
    assert ada.json()["scoreBreakdown"]["sharedCourses"] == ["Bio 101"]

    second = await client.get(f"/api/alumni/{ALUM_A}/timeline", headers=auth(other))
    assert second.status_code == 200
    assert second.json()["scoreBreakdown"]["sharedCourses"] == []


async def test_saved_paths_are_scored_against_the_caller(client, seeded):
    """The batched list must produce what the per-alumnus detail route does.

    `GET /api/paths` scores every saved path in one pass rather than one call
    apiece; this pins the two together so the list and the panel can't drift.
    """
    created = await client.post("/api/paths", json={"alumnusId": ALUM_A}, headers=auth(TOKEN_A))
    assert created.status_code == 201

    listed = await client.get("/api/paths", headers=auth(TOKEN_A))
    assert listed.status_code == 200
    detail = await client.get(f"/api/alumni/{ALUM_A}/timeline", headers=auth(TOKEN_A))
    assert listed.json()[0]["alumnus"] == detail.json()


async def test_precompute_refuses_a_corpus_from_another_school(seeded):
    """The batch job builds one corpus per school and reuses it for that school's
    students. That is only safe while the pairing is checked — a mis-grouped
    batch would write a cross-tenant constellation into Redis, and the route
    would serve it on the next hit without ever touching the corpus."""
    async with SessionLocal() as session:
        other_school = await load_corpus(session, SCHOOL_B)
        student = (
            await session.execute(select(Student).where(Student.id == STUDENT_A))
        ).scalar_one()

        with pytest.raises(ValueError, match="school"):
            await warm_student(session, student, other_school)


async def test_precompute_refuses_an_unscoped_corpus(seeded):
    """`list_alumni(school_id=None)` means every school. A tenantless student
    must not reach it — the same fail-closed rule `current_student` enforces."""
    async with SessionLocal() as session:
        with pytest.raises(ValueError, match="unscoped"):
            await load_corpus(session, None)


async def test_precomputed_constellation_only_contains_your_school(client, seeded):
    """The cached copy has to hold the same boundary the live response does."""
    async with SessionLocal() as session:
        student = (
            await session.execute(select(Student).where(Student.id == STUDENT_A))
        ).scalar_one()
        corpus = build_corpus(
            (await load_corpus(session, SCHOOL_A)).alumni, school_id=SCHOOL_A
        )
        await warm_student(session, student, corpus)

    served = await client.get("/api/constellation", headers=auth(TOKEN_A))
    assert served.status_code == 200
    assert served.json()["meta"]["cached"] is True
    assert ALUM_B not in {a["id"] for a in served.json()["alumni"]}


async def test_constellation_only_contains_your_school(client, seeded):
    response = await client.get("/api/constellation?refresh=true", headers=auth(TOKEN_A))
    assert response.status_code == 200
    alumni_ids = {a["id"] for a in response.json()["alumni"]}
    assert ALUM_B not in alumni_ids


async def test_cannot_bookmark_another_schools_alumnus(client, seeded):
    response = await client.post("/api/paths", json={"alumnusId": ALUM_B}, headers=auth(TOKEN_A))
    assert response.status_code == 404


async def test_saved_paths_are_per_student(client, seeded):
    created = await client.post("/api/paths", json={"alumnusId": ALUM_A}, headers=auth(TOKEN_A))
    assert created.status_code == 201
    path_id = created.json()["id"]

    # B sees none of A's bookmarks...
    listed = await client.get("/api/paths", headers=auth(TOKEN_B))
    assert listed.status_code == 200
    assert listed.json() == []

    # ...and cannot delete one by guessing its id.
    deleted = await client.delete(f"/api/paths/{path_id}", headers=auth(TOKEN_B))
    assert deleted.status_code == 404

    still_there = await client.get("/api/paths", headers=auth(TOKEN_A))
    assert [p["id"] for p in still_there.json()] == [path_id]


async def test_simulate_runs_as_the_caller(client, seeded):
    response = await client.post(
        "/api/simulate", json={"toMajor": "Health Policy"}, headers=auth(TOKEN_A)
    )
    assert response.status_code == 200
    for match in response.json()["matches"]:
        assert match["alumnus"]["id"] != ALUM_B


# --------------------------------------------------------------------------
# Registration and profile
# --------------------------------------------------------------------------


async def test_register_issues_a_working_token(client, seeded):
    response = await client.post(
        "/api/students/register",
        json={"schoolId": SCHOOL_A, "email": "new@test-sec.example.edu", "name": "Cam"},
    )
    assert response.status_code == 201
    payload = response.json()
    token = payload["token"]

    me = await client.get("/api/students/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["id"] == payload["student"]["id"]
    assert me.json()["schoolId"] == SCHOOL_A


async def test_register_stores_only_the_hash(client, seeded):
    response = await client.post(
        "/api/students/register",
        json={"schoolId": SCHOOL_A, "email": "hashed@test-sec.example.edu"},
    )
    token = response.json()["token"]
    student_id = response.json()["student"]["id"]

    async with SessionLocal() as session:
        stored = await session.get(Student, student_id)
        assert stored.auth_token_hash == hash_token(token)
        assert stored.auth_token_hash != token


async def test_register_rejects_unknown_school(client, seeded):
    response = await client.post(
        "/api/students/register",
        json={"schoolId": "no-such-school", "email": "x@test-sec.example.edu"},
    )
    assert response.status_code == 404


async def test_register_rejects_duplicate_email(client, seeded):
    response = await client.post(
        "/api/students/register",
        json={"schoolId": SCHOOL_A, "email": "ada@test-sec.example.edu"},
    )
    assert response.status_code == 409


async def test_register_rejects_malformed_email(client, seeded):
    response = await client.post(
        "/api/students/register", json={"schoolId": SCHOOL_A, "email": "not-an-email"}
    )
    assert response.status_code == 422


async def test_profile_update_is_partial(client, seeded):
    updated = await client.put(
        "/api/students/me", json={"major": "Public Health"}, headers=auth(TOKEN_A)
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["major"] == "Public Health"
    # Untouched fields survive a partial update.
    assert body["intendedDirection"] == "Health Policy"
    assert body["interests"] == ["Global Health Club"]


async def test_major_update_writes_the_program_row(client, seeded):
    """The major has to land in `student_program`, not just the deprecated
    scalar — `student_majors()` reads the join table, and the scalar fallback
    would otherwise mask a write that never happened."""
    await client.put("/api/students/me", json={"major": "Public Health"}, headers=auth(TOKEN_A))

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(StudentProgram).where(StudentProgram.student_id == STUDENT_A)
            )
        ).scalars().all()
    assert [(r.name, r.role.value) for r in rows] == [("Public Health", "primary")]


async def test_profile_update_can_clear_a_field(client, seeded):
    updated = await client.put(
        "/api/students/me", json={"intendedDirection": None}, headers=auth(TOKEN_A)
    )
    assert updated.status_code == 200
    assert updated.json()["intendedDirection"] is None


async def test_courses_replace_rather_than_merge(client, seeded):
    response = await client.put(
        "/api/students/me/courses",
        json={"courses": [{"code": "PH 310", "name": "Epidemiology", "semesterIndex": 3}]},
        headers=auth(TOKEN_A),
    )
    assert response.status_code == 200
    codes = [c["code"] for c in response.json()["courses"]]
    assert codes == ["PH 310"]  # the seeded BIO 101 is gone


async def test_profile_update_cannot_change_school(client, seeded):
    """`schoolId` is not an accepted field; sending it must not move the student."""
    response = await client.put(
        "/api/students/me", json={"schoolId": SCHOOL_B}, headers=auth(TOKEN_A)
    )
    assert response.status_code == 200
    assert response.json()["schoolId"] == SCHOOL_A


# --------------------------------------------------------------------------
# Admin
# --------------------------------------------------------------------------


async def test_admin_is_disabled_when_no_key_is_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "")
    response = await client.delete("/api/admin/cache")
    assert response.status_code == 503


async def test_admin_rejects_a_wrong_key(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "the-real-key")
    response = await client.delete("/api/admin/cache", headers={"X-Admin-Key": "guess"})
    assert response.status_code == 403


async def test_admin_rejects_a_student_token(client, seeded, monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "the-real-key")
    response = await client.post("/api/admin/recompute", headers=auth(TOKEN_A))
    assert response.status_code == 403
