"""Unit tests for the typeahead search endpoint.

Everything here runs without Postgres: the SQL lives in `app/repository.py` and
is exercised by the DB-guarded suite in `test_api_security.py` (including the
tenant boundary), while the query normalization, ordering, and row formatting
that decide what a student actually sees are pure and tested here.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app import repository
from app.auth import Principal, current_principal
from app.main import app
from app.matching import build_clusters, score_corpus
from app.matching.clustering import slugify
from app.search import (
    KIND_ALUMNUS,
    KIND_CLUSTER,
    KIND_MAJOR,
    MIN_QUERY_LENGTH,
    SearchHit,
    alumnus_hit,
    cluster_hit,
    like_pattern,
    major_hit,
    matches,
    normalize_query,
    order_hits,
)
from tests.factories import make_alumnus, make_profile

# --------------------------------------------------------------------------
# Query handling
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Bio  ", "Bio"),
        ("public   health", "public health"),
        ("\tPublic\nHealth\n", "Public Health"),
        ("   ", ""),
        (None, ""),
    ],
)
def test_normalize_query_trims_and_collapses(raw, expected):
    assert normalize_query(raw) == expected


class TestLikePattern:
    """A pattern is built from user input on every keystroke, so the wildcards
    have to be neutralized — an unescaped `%` matches the entire corpus."""

    def test_wraps_in_wildcards(self):
        assert like_pattern("bio") == "%bio%"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("100%", r"%100\%%"),
            ("a_b", r"%a\_b%"),
            ("back\\slash", r"%back\\slash%"),
            ("%", r"%\%%"),
        ],
    )
    def test_escapes_wildcards(self, raw, expected):
        assert like_pattern(raw) == expected

    def test_matches_mirrors_the_sql_filter(self):
        """`matches` picks the course a row displays; it has to agree with the
        `ILIKE` that selected the row, or the row names a course that didn't
        match. With the wildcards escaped, that filter is a substring test."""
        assert matches("bio", "BIO 101")
        assert matches("BIO", "bio 101")
        assert not matches("%", "BIO 101")
        assert not matches("chem", "BIO 101")


# --------------------------------------------------------------------------
# Row construction
# --------------------------------------------------------------------------


def test_major_and_cluster_rows_pluralize_their_count():
    assert major_hit("Biochemistry", 1, 0.4).detail == "1 alumnus"
    assert major_hit("Biochemistry", 12, 0.4).detail == "12 alumni"
    assert cluster_hit("Health Policy", 3, 0.4, None).detail == "3 alumni"


def test_cluster_row_id_matches_the_constellation_cluster_id():
    """A row the student clicks has to name the cluster the map already drew.
    Two id schemes for the same cluster would leave the frontend reconciling
    them, which is where a "no such cluster" bug lives."""
    alumnus = make_alumnus("a1", career_area="Health Policy", industry="Health Policy")
    scored = score_corpus(make_profile(), [alumnus])
    cluster = build_clusters(scored)[0]

    assert cluster_hit(cluster.label, 1, 0.5, None).id == cluster.id


def test_major_row_id_is_the_slug_and_the_label_is_the_facet_value():
    hit = major_hit("Public Health", 4, 0.6)
    assert hit.id == slugify("Public Health") == "public-health"
    # Explore's `?major=` facet is free text, so the label is what goes back.
    assert hit.label == "Public Health"


def test_cluster_row_carries_synthetic_provenance():
    hit = cluster_hit("Health Policy", 9, 0.5, "synthetic")
    assert hit.provenance == "synthetic"


class TestAlumnusRow:
    def _alumnus(self):
        return make_alumnus(
            "alum-1",
            graduation_year=2022,
            origin_major="Biochemistry",
            final_major="Public Health",
            courses=[("BIO 101", "Bio 101", 0), ("PH 201", "Intro Public Health", 3)],
            career_area="Health Policy",
            industry="Health Policy",
            occupation="Policy Analyst",
        )

    def test_label_is_the_class_year(self):
        """Alumni have no name column by construction — there is nothing else
        this row could be called."""
        assert alumnus_hit(self._alumnus(), 0.5, "bio").label == "Class of 2022"

    def test_detail_names_the_path_and_the_matched_course(self):
        hit = alumnus_hit(self._alumnus(), 0.5, "bio")
        assert hit.detail == "Biochemistry → Public Health · Bio 101"

    def test_detail_never_mentions_the_career_outcome(self):
        """The outcome is `provenance='synthetic'` on the placeholder dataset and
        a one-line search hint has nowhere to put the flag that says so."""
        hit = alumnus_hit(self._alumnus(), 0.5, "bio")
        assert "Policy Analyst" not in hit.detail
        assert "Health Policy" not in hit.detail
        assert hit.provenance is None

    def test_matched_course_is_the_earliest_matching_term(self):
        alumnus = make_alumnus(
            "alum-2",
            courses=[("PH 301", "Health Systems", 5), ("PH 201", "Intro PH", 3)],
        )
        assert alumnus_hit(alumnus, 0.5, "ph").detail.endswith("Intro PH")

    def test_alumnus_without_a_matching_course_still_renders(self):
        alumnus = make_alumnus("alum-3", courses=[("BIO 101", "Bio 101", 0)])
        hit = alumnus_hit(alumnus, 0.5, "zzz")
        assert hit.detail and "Bio 101" not in hit.detail


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------


def _hit(kind: str, hit_id: str, score: float, label: str | None = None) -> SearchHit:
    return SearchHit(kind=kind, id=hit_id, label=label or hit_id, detail=None, score=score)


class TestOrdering:
    def test_destinations_come_before_people(self):
        """Even when an alumnus scores higher. The first row is what Enter
        selects, and a major is a place the student can go; an alumnus is one
        path among the hundreds that contain the course they typed."""
        ordered = order_hits(
            [
                _hit(KIND_ALUMNUS, "alum-1", 0.9),
                _hit(KIND_CLUSTER, "health-policy", 0.2),
                _hit(KIND_MAJOR, "biology", 0.1),
            ]
        )
        assert [h.kind for h in ordered] == [KIND_MAJOR, KIND_CLUSTER, KIND_ALUMNUS]

    def test_similarity_orders_within_a_kind(self):
        ordered = order_hits(
            [
                _hit(KIND_MAJOR, "molecular-biology", 0.2),
                _hit(KIND_MAJOR, "biology", 0.8),
                _hit(KIND_MAJOR, "biochemistry", 0.5),
            ]
        )
        assert [h.id for h in ordered] == ["biology", "biochemistry", "molecular-biology"]

    def test_ties_break_totally(self):
        """Everyone who took the matched course scores identically, so without a
        total tie-break the same query returns the same rows in a different
        order on the next keystroke."""
        tied = [_hit(KIND_ALUMNUS, f"alum-{i}", 0.4, "Class of 2022") for i in (3, 1, 2)]
        assert [h.id for h in order_hits(tied)] == ["alum-1", "alum-2", "alum-3"]
        assert order_hits(tied) == order_hits(list(reversed(tied)))


# --------------------------------------------------------------------------
# Scoping — the queries refuse to run unscoped
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query_fn",
    [
        repository.search_majors,
        repository.search_career_clusters,
        repository.search_alumni_by_course,
    ],
)
@pytest.mark.parametrize("school_id", [None, ""])
async def test_search_queries_refuse_an_unscoped_school(query_fn, school_id):
    """`school_id=None` means *every school* to `list_alumni`, which is the
    fail-open default the auth layer keeps off the request path. A search has no
    offline caller with a reason to want it, so it raises — before it opens a
    session, which is why this needs no database."""
    with pytest.raises(ValueError, match="scoped"):
        await query_fn(None, "biology", school_id=school_id, limit=5)


# --------------------------------------------------------------------------
# Route — the paths that answer without touching the database
# --------------------------------------------------------------------------


@pytest.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


@pytest.fixture
def as_student():
    """Stand in for the token holder. The tenant boundary itself is enforced in
    SQL and tested against a live Postgres in `test_api_security.py`; overriding
    it here keeps the request-shaping tests off the database."""
    app.dependency_overrides[current_principal] = lambda: Principal(
        id="stu-1", school_id="school-a"
    )
    yield
    app.dependency_overrides.pop(current_principal, None)


async def test_search_requires_a_token(client):
    assert (await client.get("/api/search?q=biology")).status_code == 401


@pytest.mark.parametrize("query", ["b", "bi", "  b  ", ""])
async def test_short_queries_answer_empty_without_a_query(client, as_student, query):
    """Below the trigram threshold an `ILIKE '%q%'` cannot use the GIN indexes,
    so every keystroke would scan the corpus for a prefix matching most of it.
    This must return before it opens a session — the assertion that it does is
    that this test passes with no Postgres running."""
    response = await client.get("/api/search", params={"q": query})
    assert response.status_code == 200
    assert response.json() == {"query": normalize_query(query), "results": [], "total": 0}


def test_min_query_length_is_the_trigram_threshold():
    """Three characters is the shortest `%q%` pattern containing a whole
    trigram, i.e. the shortest one the GIN indexes can serve."""
    assert MIN_QUERY_LENGTH == 3


async def test_a_missing_query_is_a_422(client, as_student):
    assert (await client.get("/api/search")).status_code == 422


async def test_an_overlong_query_is_rejected(client, as_student):
    assert (await client.get("/api/search", params={"q": "x" * 65})).status_code == 422
