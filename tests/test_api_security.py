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

from app import cache
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
# School B's alumnus with vocabulary that appears nowhere else, so a search run
# as School A either finds it or the tenant boundary has a hole.
SEARCH_ALUM_B = "test-sec-search-alum-b"
SEARCH_MAJOR_B = "Test Sec Xenobiology"
SEARCH_AREA_B = "Test Sec Xenopolicy"
SEARCH_COURSE_B = "XENO 999"
SEARCH_TERM = "xeno"
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


def _alumnus(
    alumnus_id: str,
    school_id: str,
    career_area: str,
    major: str = "Biochemistry",
    courses: list[tuple[str, str, int]] | None = None,
) -> Alumnus:
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
        AlumnusCourse(course_code=code, course_name=name, semester_index=semester)
        for code, name, semester in courses
        or [
            ("BIO 101", "Bio 101", 0),
            ("PH 201", "Intro to Public Health", 3),
        ]
    ]
    alumnus.majors = [AlumnusMajor(name=major, declared_semester=1, is_final=True)]
    alumnus.outcomes = [
        CareerOutcome(industry=career_area, occupation="Analyst", years_post_grad=1)
    ]
    return alumnus


async def _cleanup() -> None:
    async with SessionLocal() as session:
        await session.execute(delete(Student).where(Student.id.in_([STUDENT_A, STUDENT_B])))
        await session.execute(
            delete(Alumnus).where(Alumnus.id.in_([ALUM_A, ALUM_B, SEARCH_ALUM_B]))
        )
        # Any student registered through the API during a test.
        await session.execute(delete(Student).where(Student.school_id.in_([SCHOOL_A, SCHOOL_B])))
        await session.execute(delete(School).where(School.id.in_([SCHOOL_A, SCHOOL_B])))
        await session.commit()

    # Resolved tokens are cached for `auth_cache_ttl_seconds`. These fixtures
    # recreate the same tokens against freshly inserted rows, so a principal
    # left over from the previous test would outlive the student it names.
    for token in (TOKEN_A, TOKEN_B, "test-sec-orphan-token"):
        try:
            await cache.forget_principal(hash_token(token))
        except Exception:
            pass


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
                _alumnus(
                    SEARCH_ALUM_B,
                    SCHOOL_B,
                    SEARCH_AREA_B,
                    major=SEARCH_MAJOR_B,
                    courses=[(SEARCH_COURSE_B, "Xenobiology Seminar", 4)],
                ),
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


def _alumni_ids(payload: dict) -> set[str]:
    """Alumni live inside their cluster now — the grouping is the layout."""
    return {a["id"] for cluster in payload["clusters"] for a in cluster["alumni"]}


# --------------------------------------------------------------------------
# 401 — no token, no access
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/constellation", None),
        ("get", f"/api/alumni/{ALUM_A}/timeline", None),
        ("post", "/api/simulate", {"toMajor": "Health Policy"}),
        ("get", "/api/search?q=biology", None),
        ("get", "/api/paths", None),
        ("post", "/api/paths", {"alumnusId": ALUM_A}),
        ("post", "/api/paths/combine", {"pathIds": [1, 2]}),
        ("get", "/api/students/me", None),
        ("get", "/api/students/me/activity", None),
        ("put", "/api/students/me", {"year": "junior"}),
        ("put", "/api/students/me/courses", {"courses": []}),
        ("get", "/api/students/me/dashboard", None),
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

        # And again, because the token cache must not learn a tenantless
        # principal. Caching one would turn a fail-closed check into a
        # fail-open one for the length of the TTL.
        again = await client.get(
            "/api/constellation", headers=auth("test-sec-orphan-token")
        )
        assert again.status_code == 403
        assert await cache.get_principal(hash_token("test-sec-orphan-token")) is None
    finally:
        async with SessionLocal() as session:
            await session.execute(delete(Student).where(Student.id == "test-sec-orphan"))
            await session.commit()


async def test_cached_token_still_scopes_to_its_own_school(client, seeded):
    """The auth cache stores (student, school). If it ever dropped the school —
    or served one token's entry for another — the corpus boundary would move
    with it, so exercise both tokens repeatedly through the cached path."""
    for _ in range(3):
        a = await client.get("/api/constellation", headers=auth(TOKEN_A))
        b = await client.get("/api/constellation", headers=auth(TOKEN_B))
        assert a.status_code == 200 and b.status_code == 200
        assert ALUM_B not in _alumni_ids(a.json())
        assert ALUM_A not in _alumni_ids(b.json())

    assert await cache.get_principal(hash_token(TOKEN_A)) == (STUDENT_A, SCHOOL_A)
    assert await cache.get_principal(hash_token(TOKEN_B)) == (STUDENT_B, SCHOOL_B)


async def test_an_invalid_token_is_never_cached_as_valid(client, seeded):
    for _ in range(3):
        assert (
            await client.get("/api/constellation", headers=auth("not-a-real-token"))
        ).status_code == 401
    assert await cache.get_principal(hash_token("not-a-real-token")) is None


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
    assert ALUM_B not in _alumni_ids(served.json())


async def test_constellation_only_contains_your_school(client, seeded):
    response = await client.get("/api/constellation?refresh=true", headers=auth(TOKEN_A))
    assert response.status_code == 200
    alumni_ids = _alumni_ids(response.json())
    assert ALUM_B not in alumni_ids


def _search(client, token: str, query: str):
    return client.get("/api/search", params={"q": query}, headers=auth(token))


async def test_search_never_crosses_the_school_boundary(client, seeded):
    """The whole point of the typeahead is to name what exists. School B has an
    alumnus whose major, career area, and course code appear nowhere in School
    A's corpus; searching for them as A must come back empty, because a row here
    would confirm the existence of records A is not allowed to read.

    Run as B first: an empty result for A proves nothing if the term matches
    nothing anywhere.
    """
    theirs = await _search(client, TOKEN_B, SEARCH_TERM)
    assert theirs.status_code == 200
    found = theirs.json()["results"]
    assert {r["type"] for r in found} == {"major", "cluster", "alumnus"}
    assert {r["label"] for r in found} >= {SEARCH_MAJOR_B, SEARCH_AREA_B}
    assert SEARCH_ALUM_B in {r["id"] for r in found}

    mine = await _search(client, TOKEN_A, SEARCH_TERM)
    assert mine.status_code == 200
    assert mine.json() == {"query": SEARCH_TERM, "results": [], "total": 0}


async def test_search_alumni_rows_are_only_your_school(client, seeded):
    """Both schools' alumni took BIO 101, so this is the case where a missing
    `school_id` on the course query would silently return the other tenant's
    ids — the same corpus boundary the constellation holds."""
    response = await _search(client, TOKEN_A, "bio 101")
    assert response.status_code == 200
    ids = {r["id"] for r in response.json()["results"] if r["type"] == "alumnus"}
    assert ids == {ALUM_A}


async def test_search_labels_a_synthetic_career_outcome(client, seeded):
    """The cluster label comes from seeded employment data on this corpus. It
    reaches the UI here, so it has to carry the flag that says it is a
    placeholder — the same rule the constellation node follows."""
    response = await _search(client, TOKEN_B, SEARCH_TERM)
    clusters = [r for r in response.json()["results"] if r["type"] == "cluster"]
    assert [c["provenance"] for c in clusters] == ["synthetic"]
async def test_dashboard_only_counts_your_school(client, seeded):
    """The stats are a projection of the constellation, so they inherit its
    boundary — but a projection is new code, and a boundary that holds only in
    the route it was first written for is not a boundary."""
    try:
        await cache.invalidate_student(STUDENT_A)
    except Exception:
        pass  # Redis down — then the route computes inline, which is the point.

    response = await client.get("/api/students/me/dashboard", headers=auth(TOKEN_A))
    assert response.status_code == 200
    body = response.json()
    assert {match["id"] for match in body["topMatches"]} == {ALUM_A}
    assert body["stats"]["alumniMatches"] == 1


async def test_dashboard_counts_only_your_own_saved_paths(client, seeded):
    created = await client.post("/api/paths", json={"alumnusId": ALUM_A}, headers=auth(TOKEN_A))
    assert created.status_code == 201

    mine = await client.get("/api/students/me/dashboard", headers=auth(TOKEN_A))
    theirs = await client.get("/api/students/me/dashboard", headers=auth(TOKEN_B))
    assert mine.json()["stats"]["savedPaths"] == 1
    assert theirs.json()["stats"]["savedPaths"] == 0


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
    # Cards carry the alumnus id so they can open the same detail panel a
    # constellation node does — which also means the tenant boundary has to
    # hold here exactly as it does there.
    for card in response.json()["cards"]:
        assert card["id"] != ALUM_B


async def test_profile_matches_the_frontend_contract(client, seeded):
    """The Dashboard and sidebar read this payload directly."""
    response = await client.get("/api/students/me", headers=auth(TOKEN_A))
    assert response.status_code == 200
    body = response.json()

    assert set(body) >= {
        "id",
        "schoolId",
        "school",
        "firstName",
        "lastName",
        "email",
        "currentYear",
        "declaredMajor",
        "interests",
        "coursesCompleted",
    }
    assert body["firstName"] == "Ada"
    assert body["school"] == "Test Security School A"
    assert body["currentYear"] == "sophomore"

    course = body["coursesCompleted"][0]
    assert set(course) == {"id", "name", "semester", "semesterIndex"}
    # The label is derived from the index, which stays the source of truth for
    # timing — the client never has to parse a label back into an ordering.
    assert course["semesterIndex"] == 0
    assert course["semester"] == "Freshman Fall"


async def test_activity_feed_is_per_student(client, seeded):
    """A feed is a record of one person's actions. There is no route that takes
    a student id, so reading someone else's is unexpressible rather than denied
    — this pins that the rows are scoped too."""
    await client.put(
        "/api/students/me", json={"intendedDirection": "Health Policy"}, headers=auth(TOKEN_A)
    )

    mine = await client.get("/api/students/me/activity", headers=auth(TOKEN_A))
    theirs = await client.get("/api/students/me/activity", headers=auth(TOKEN_B))
    assert mine.status_code == 200 and theirs.status_code == 200
    assert len(mine.json()) == 1
    assert theirs.json() == []


async def test_activity_collapses_a_burst_of_identical_actions(client, seeded):
    """Toggling the same control three times should leave one line, not three."""
    for _ in range(3):
        await client.put(
            "/api/students/me", json={"intendedDirection": "Health Policy"}, headers=auth(TOKEN_A)
        )
    feed = (await client.get("/api/students/me/activity", headers=auth(TOKEN_A))).json()
    assert [e["kind"] for e in feed] == ["updated_profile"]


async def test_activity_is_newest_first_and_capped(client, seeded):
    await client.put("/api/students/me", json={"year": "junior"}, headers=auth(TOKEN_A))
    await client.put(
        "/api/students/me/courses",
        json={"courses": [{"code": "PH 310", "name": "Epidemiology", "semesterIndex": 3}]},
        headers=auth(TOKEN_A),
    )
    feed = (await client.get("/api/students/me/activity?limit=1", headers=auth(TOKEN_A))).json()
    assert len(feed) == 1
    assert feed[0]["kind"] == "updated_courses"


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
    assert body["declaredMajor"] == "Public Health"
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
    # `id` is the course code, which is what PUT /me/courses takes as `code`.
    codes = [c["id"] for c in response.json()["coursesCompleted"]]
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
