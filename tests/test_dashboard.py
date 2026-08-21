"""The Dashboard endpoint — the stat cards and the top-match list.

None of this needs Postgres or Redis. The projection is a pure function over a
constellation payload, and the route tests stub the two things it reads (the
cached entry and the saved-path count) so the wiring, the tenant scoping, and
the degraded-Redis path are all exercised in memory.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import cache, repository
from app.api.routes import dashboard as dashboard_route
from app.api.routes.dashboard import broad_query, build_dashboard
from app.auth import Principal, current_principal
from app.config import settings
from app.db import get_session
from app.jobs.recompute import ExploreQuery
from app.main import app
from app.matching import build_constellation_for_query
from app.models import Student, StudentYear
from tests.factories import make_alumnus, make_profile

SCHOOL = "test-school"


def _corpus() -> list:
    return [
        make_alumnus(
            "a1",
            career_area="Health Policy",
            origin_major="Biochemistry",
            final_major="Public Health",
            industry="Health Policy",
            courses=[("BIO 101", "Bio 101", 0), ("CHEM 101", "Chem 101", 1)],
        ),
        make_alumnus(
            "a2",
            career_area="Software",
            origin_major="Computer Science",
            final_major="Computer Science",
            pivot_semester=None,
            industry="Software",
            courses=[("CS 101", "Intro CS", 0)],
        ),
        make_alumnus(
            "a3",
            career_area="Health Policy",
            origin_major="Biology",
            final_major="Public Health",
            industry="Health Policy",
            courses=[("BIO 101", "Bio 101", 0)],
        ),
    ]


def _payload(alumni: list | None = None) -> dict:
    response = build_constellation_for_query(
        make_profile(), _corpus() if alumni is None else alumni
    )
    return response.model_dump(by_alias=True)


def _all_alumni(payload: dict) -> list[dict]:
    return [a for cluster in payload["clusters"] for a in cluster["alumni"]]


def _node(alumnus_id: str, score: float) -> dict:
    return {
        "id": alumnus_id,
        "similarityScore": score,
        "graduationYear": 2022,
        "cluster": "Health Policy",
        "majors": ["Public Health"],
        "careerOutcome": {"title": "Analyst", "org": "Agency"},
    }


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------


class TestBuildDashboard:
    def test_stats_restate_the_constellation(self):
        """Every stat is the constellation's own number, not a recount.

        The dashboard and the map are the same data; a card that disagreed with
        the view it links to is worse than no card.
        """
        payload = _payload()
        result = build_dashboard(payload, saved_paths=2, limit=4, cached=False)

        assert result.stats.alumni_matches == payload["totalAlumni"]
        assert result.stats.clusters == len(payload["clusters"])
        assert result.stats.saved_paths == 2

    def test_highest_match_is_the_top_score(self):
        payload = _payload()
        result = build_dashboard(payload, saved_paths=0, limit=4, cached=False)
        assert result.stats.highest_match == max(
            a["similarityScore"] for a in _all_alumni(payload)
        )

    def test_top_matches_are_ranked_across_clusters(self):
        """The payload groups alumni by cluster; this list is flat, so the
        ranking has to be rebuilt rather than read off the first cluster."""
        payload = _payload()
        result = build_dashboard(payload, saved_paths=0, limit=10, cached=False)
        scores = [m.similarity_score for m in result.top_matches]
        assert scores == sorted(scores, reverse=True)
        assert len(result.top_matches) == len(_all_alumni(payload))

    def test_ties_break_on_id(self):
        """Same rule as the scorer's. The frontend's spatial memory depends on
        the same query producing the same order every time."""
        payload = {
            "totalAlumni": 3,
            "clusters": [
                {"alumni": [_node("a-z", 80.0), _node("a-a", 80.0)]},
                {"alumni": [_node("a-m", 80.0)]},
            ],
            "meta": {"generatedAt": "2026-08-20T00:00:00+00:00"},
        }
        result = build_dashboard(payload, saved_paths=0, limit=3, cached=True)
        assert [m.id for m in result.top_matches] == ["a-a", "a-m", "a-z"]

    def test_limit_cuts_the_list_not_the_stats(self):
        payload = _payload()
        result = build_dashboard(payload, saved_paths=0, limit=1, cached=False)
        assert len(result.top_matches) == 1
        assert result.stats.alumni_matches == payload["totalAlumni"]

    def test_empty_constellation_has_no_highest_match(self):
        """None, not 0.0 — a 0 reads as "your best match is 0%" when what
        actually happened is that nothing matched."""
        result = build_dashboard(_payload(alumni=[]), saved_paths=0, limit=4, cached=False)
        assert result.stats.highest_match is None
        assert result.stats.alumni_matches == 0
        assert result.stats.clusters == 0
        assert result.top_matches == []

    def test_top_match_keeps_outcome_provenance(self):
        """These outcomes are `provenance='synthetic'` on the placeholder
        dataset. A card is exactly where a trimmed payload would drop the flag
        and present invented employment as reported fact."""
        result = build_dashboard(_payload(), saved_paths=0, limit=4, cached=False)
        assert result.top_matches
        assert all(m.career_outcome.provenance == "synthetic" for m in result.top_matches)

    def test_carries_no_geometry(self):
        """Same invariant as the constellation: radius, angle, and coordinates
        belong to the frontend."""
        blob = build_dashboard(
            _payload(), saved_paths=0, limit=4, cached=False
        ).model_dump_json(by_alias=True)
        for banned in ('"x"', '"y"', "radius", "angle", "coordinates"):
            assert banned not in blob


# --------------------------------------------------------------------------
# Which cache entry this reads
# --------------------------------------------------------------------------


class TestBroadQuery:
    def test_matches_the_default_constellation_request(self):
        """The dashboard summarizes the entry `/api/constellation` writes and
        the nightly job warms. A different hash would mean a second entry
        holding the identical payload, warmed by nobody."""
        assert (
            broad_query().cache_hash()
            == ExploreQuery.build(max_alumni=settings.constellation_max_alumni).cache_hash()
        )

    def test_is_the_unfiltered_query(self):
        assert broad_query().is_broad


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


def _student(student_id: str) -> Student:
    return Student(id=student_id, school_id=SCHOOL, year=StudentYear.sophomore, interests=[])


@pytest.fixture
def client():
    """The app with auth and the session stubbed out.

    `get_session` yields a sentinel: every repository call the dashboard makes
    is patched in these tests, so nothing ever touches it — the override is what
    keeps the route off Postgres.
    """

    async def _session():
        yield object()

    app.dependency_overrides[current_principal] = lambda: Principal(
        id="stu-dash", school_id=SCHOOL
    )
    app.dependency_overrides[get_session] = _session
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    app.dependency_overrides.clear()


@pytest.fixture
def no_saved_paths(monkeypatch):
    async def _list(session, student_id):
        return []

    monkeypatch.setattr(repository, "list_saved_paths", _list)


@pytest.fixture
def corpus_from_memory(monkeypatch):
    """Stand in for the two reads the miss path makes against Postgres."""

    async def _get_student(session, student_id):
        return _student(student_id)

    async def _list_alumni(session, school_id=None):
        return _corpus()

    monkeypatch.setattr(repository, "get_student", _get_student)
    monkeypatch.setattr(repository, "list_alumni", _list_alumni)


class TestDashboardRoute:
    async def test_serves_the_cached_constellation(self, client, monkeypatch):
        payload = _payload()
        seen: list[str] = []

        async def _get_raw(key):
            seen.append(key)
            return cache.serialize_cached(_payload())

        async def _list(session, student_id):
            return [object(), object()]

        monkeypatch.setattr(cache, "get_raw", _get_raw)
        monkeypatch.setattr(repository, "list_saved_paths", _list)

        response = await client.get("/api/students/me/dashboard")
        body = response.json()

        assert response.status_code == 200
        assert seen == [cache.constellation_key("stu-dash", broad_query().cache_hash())]
        assert body["meta"]["cached"] is True
        assert body["stats"]["alumniMatches"] == payload["totalAlumni"]
        assert body["stats"]["savedPaths"] == 2
        assert len(body["topMatches"]) == min(4, len(_all_alumni(payload)))

    async def test_top_matches_does_not_fork_the_cache_key(
        self, client, monkeypatch, no_saved_paths
    ):
        """`topMatches` slices a list the entry already holds in full. In the
        key it would fork one payload into twenty identical entries — and a
        request for four would write an entry the constellation route then
        serves as the whole map."""
        seen: list[str] = []

        async def _get_raw(key):
            seen.append(key)
            return cache.serialize_cached(_payload())

        monkeypatch.setattr(cache, "get_raw", _get_raw)

        for limit in (1, 2, 9):
            response = await client.get(f"/api/students/me/dashboard?topMatches={limit}")
            assert len(response.json()["topMatches"]) == min(limit, 3)
        assert len(set(seen)) == 1

    async def test_computes_inline_when_redis_is_down(
        self, client, monkeypatch, no_saved_paths
    ):
        """A cold or unavailable Redis costs latency, not availability."""
        scoped_to: list[str | None] = []

        async def _boom(*args, **kwargs):
            raise ConnectionError("redis down")

        async def _get_student(session, student_id):
            return _student(student_id)

        async def _list_alumni(session, school_id=None):
            scoped_to.append(school_id)
            return _corpus()

        monkeypatch.setattr(cache, "get_raw", _boom)
        monkeypatch.setattr(cache, "set_raw", _boom)
        monkeypatch.setattr(repository, "get_student", _get_student)
        monkeypatch.setattr(repository, "list_alumni", _list_alumni)

        response = await client.get("/api/students/me/dashboard")

        assert response.status_code == 200
        assert response.json()["meta"]["cached"] is False
        # The corpus is the tenant boundary: an alumnus from another school
        # cannot reach a stat, let alone a card.
        assert scoped_to == [SCHOOL]

    async def test_backfills_the_constellation_entry(
        self, client, monkeypatch, no_saved_paths, corpus_from_memory
    ):
        """A student who lands on the dashboard first has warmed Explore by the
        time they click through."""
        written: dict[str, bytes] = {}
        tracked: list[dict] = []

        async def _get_raw(key):
            return None

        async def _set_raw(key, blob, ttl=None):
            written[key] = blob

        async def _track(student_id, key, params=None):
            tracked.append(params or {})

        monkeypatch.setattr(cache, "get_raw", _get_raw)
        monkeypatch.setattr(cache, "set_raw", _set_raw)
        monkeypatch.setattr(cache, "track_student_key", _track)

        await client.get("/api/students/me/dashboard")

        key = cache.constellation_key("stu-dash", broad_query().cache_hash())
        assert list(written) == [key]
        assert tracked == [broad_query().as_params()]

    async def test_a_corrupt_entry_behaves_as_a_miss(
        self, client, monkeypatch, no_saved_paths, corpus_from_memory
    ):
        async def _get_raw(key):
            return b"\x1f\x8bnot actually gzip"

        async def _noop(*args, **kwargs):
            return None

        monkeypatch.setattr(cache, "get_raw", _get_raw)
        monkeypatch.setattr(cache, "set_raw", _noop)
        monkeypatch.setattr(cache, "track_student_key", _noop)

        response = await client.get("/api/students/me/dashboard")
        assert response.status_code == 200
        assert response.json()["meta"]["cached"] is False

    async def test_a_vanished_student_is_401_not_500(
        self, client, monkeypatch, no_saved_paths
    ):
        """A cached principal outlives its student by up to the auth TTL."""

        async def _get_raw(key):
            return None

        async def _get_student(session, student_id):
            return None

        monkeypatch.setattr(cache, "get_raw", _get_raw)
        monkeypatch.setattr(repository, "get_student", _get_student)

        response = await client.get("/api/students/me/dashboard")
        assert response.status_code == 401


class TestDashboardIsTokenAddressed:
    """Security invariant 1: no student-facing route accepts a student id."""

    def test_takes_no_student_id(self):
        params = app.openapi()["paths"]["/api/students/me/dashboard"]["get"]["parameters"]
        names = {p["name"].lower() for p in params}
        assert "studentid" not in names
        assert "student_id" not in names

    async def test_401_without_a_token(self):
        # No dependency override here: the real `current_principal` runs, and it
        # must reject before anything reads a cache entry or a saved path.
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as unauthenticated:
            response = await unauthenticated.get("/api/students/me/dashboard")
        assert response.status_code == 401


class TestModuleSurface:
    def test_lives_beside_the_profile_routes(self):
        # A second router under the same prefix: `/me` must still resolve to the
        # profile rather than being shadowed by this one.
        paths = app.openapi()["paths"]
        assert "/api/students/me" in paths
        assert "/api/students/me/dashboard" in paths

    def test_default_top_matches_matches_the_design(self):
        # The Dashboard renders four cards.
        assert dashboard_route.DEFAULT_TOP_MATCHES == 4
