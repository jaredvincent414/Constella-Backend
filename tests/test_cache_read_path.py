"""The cache contract and the read paths that depend on it.

None of this needs Postgres or Redis: the serialization helpers are pure, the
pipeline runs on factory-built ORM objects, and the one job function that reads
Redis is exercised through a stubbed index.
"""

from __future__ import annotations

import json

import pytest

from app import cache
from app.config import settings
from app.jobs.recompute import queries_to_warm, query_hash
from app.matching import (
    build_constellation,
    build_constellation_for_query,
    filter_by_pivot_query,
    score_corpus,
)
from tests.factories import make_alumnus, make_profile


def _corpus() -> list:
    return [
        make_alumnus(
            "a1",
            career_area="Health Policy",
            origin_major="Biochemistry",
            final_major="Public Health",
            courses=[("BIO 101", "Bio 101", 0), ("CHEM 101", "Chem 101", 1)],
        ),
        make_alumnus(
            "a2",
            career_area="Software",
            origin_major="Computer Science",
            final_major="Computer Science",
            pivot_semester=None,
            courses=[("CS 101", "Intro CS", 0)],
        ),
        make_alumnus(
            "a3",
            career_area="Health Policy",
            origin_major="Biology",
            final_major="Public Health",
            courses=[("BIO 101", "Bio 101", 0)],
        ),
    ]


# --------------------------------------------------------------------------
# What the cache stores
# --------------------------------------------------------------------------


class TestSerializeCached:
    def test_stamps_meta_cached_true(self):
        blob = cache.serialize_cached({"meta": {"cached": False, "returned": 3}})
        assert json.loads(cache.decompress(blob))["meta"]["cached"] is True

    def test_payload_without_meta_is_left_alone(self):
        # Combined paths and timelines have no `meta` block; the helper has to
        # serialize them unchanged rather than inventing one.
        blob = cache.serialize_cached({"sharedCourses": ["Bio 101"]})
        assert json.loads(cache.decompress(blob)) == {"sharedCourses": ["Bio 101"]}

    def test_is_gzip(self):
        """Stored compressed, and served that way — the browser accepts gzip, so
        the read path hands these bytes over without decompressing them."""
        blob = cache.serialize_cached({"meta": {"cached": False}})
        assert blob.startswith(b"\x1f\x8b")

    def test_is_byte_stable_for_the_same_payload(self):
        """gzip stamps its header with the current time unless told not to.

        Without a fixed mtime every rewrite of an unchanged payload produces
        different bytes, which makes "did this entry actually change?" an
        unanswerable question.
        """
        payload = {"meta": {"cached": False}, "alumni": [{"id": "a1"}]}
        assert cache.serialize_cached(dict(payload)) == cache.serialize_cached(dict(payload))

    def test_non_ascii_is_not_escaped(self):
        """The stored bytes must decompress to what FastAPI would have sent.

        Escaping would still parse identically, but it makes a hit differ
        byte-for-byte from a miss — and match reasons are full of em dashes.
        """
        blob = cache.serialize_cached({"matchReason": "Changed direction — like you"})
        text = cache.decompress(blob).decode()
        assert "—" in text
        assert "\\u2014" not in text


class TestFlushPattern:
    """A `CACHE_VERSION` bump orphans the previous keyspace rather than freeing
    it — entries stay resident until their TTL, which is now measured in days.
    The flush is the only thing that reclaims them, so it must not be scoped to
    the version that happens to be current."""

    def test_matches_entries_from_older_cache_versions(self):
        from fnmatch import fnmatch

        for version in ("v2", "v5", "v6", cache.CACHE_VERSION):
            key = f"constella:{version}:constellation:stu-1:abc123"
            assert fnmatch(key, cache.FLUSH_PATTERN), version

    def test_matches_every_kind_of_entry(self):
        from fnmatch import fnmatch

        for key in (
            cache.constellation_key("stu-1", "abc"),
            cache.timeline_key("stu-1", "alum-1"),
            cache.combined_path_key("stu-1", ["a", "b"]),
            cache.student_index_key("stu-1"),
        ):
            assert fnmatch(key, cache.FLUSH_PATTERN), key


class TestTimelineKey:
    def test_is_scoped_to_the_viewing_student(self):
        """The detail payload is a comparison against the caller's transcript.

        An alumnus-only key would serve the first student's shared-course list
        to the next student who opened the same node.
        """
        assert cache.timeline_key("stu-a", "alum-1") != cache.timeline_key("stu-b", "alum-1")

    def test_distinguishes_alumni_for_one_student(self):
        assert cache.timeline_key("stu-a", "alum-1") != cache.timeline_key("stu-a", "alum-2")


# --------------------------------------------------------------------------
# The route and the job must build the same thing for the same key
# --------------------------------------------------------------------------


def _comparable(response) -> dict:
    payload = response.model_dump(by_alias=True)
    payload["meta"].pop("generatedAt")
    return payload


class TestBuildForQuery:
    def test_matches_a_hand_applied_pivot_query(self):
        """Same result as overriding the profile and filtering by hand.

        This is the contract the cache key rests on: the job warms an entry the
        route will serve, so both sides have to mean the same thing by
        (fromMajor, toMajor, limit).
        """
        alumni = _corpus()
        manual_profile = make_profile(declared_major="Biochemistry")
        manual_profile.declared_major = "Biochemistry"
        manual_profile.current_majors = {"Biochemistry"}
        manual_profile.intended_direction = "Public Health"
        expected, _ = build_constellation(
            manual_profile,
            filter_by_pivot_query(alumni, "Biochemistry", "Public Health"),
            max_alumni=10,
        )

        actual = build_constellation_for_query(
            make_profile(declared_major="Biochemistry"),
            alumni,
            "Biochemistry",
            "Public Health",
            10,
        )
        assert _comparable(actual) == _comparable(expected)

    def test_does_not_mutate_the_callers_profile(self):
        """The job walks several queries for one student on one profile object.

        An override leaking from one variant into the next would score the
        student against the wrong hypothetical — and cache the result under a
        key that claims otherwise.
        """
        profile = make_profile(declared_major="Biochemistry", intended_direction=None)
        build_constellation_for_query(profile, _corpus(), "Economics", "Public Health", 10)

        assert profile.declared_major == "Biochemistry"
        assert profile.intended_direction is None
        assert profile.current_majors == set()

    def test_successive_queries_are_independent(self):
        profile = make_profile(declared_major="Biochemistry")
        alumni = _corpus()

        first = build_constellation_for_query(profile, alumni, None, None, 10)
        build_constellation_for_query(profile, alumni, "Economics", "Software", 10)
        again = build_constellation_for_query(profile, alumni, None, None, 10)

        assert _comparable(again) == _comparable(first)


class TestBatchScoringEquivalence:
    def test_an_alumnus_scores_the_same_alone_as_in_company(self):
        """`GET /api/paths` scores every saved path in one pass.

        That is only a safe substitution for the old per-path call if no
        component depends on which other alumni are in the corpus. If someone
        ever adds a corpus-relative component (a percentile, a z-score), this
        test is what catches that the batched route now reports different
        numbers than the detail panel.
        """
        profile = make_profile()
        alumni = _corpus()

        batched = {item.alumnus.id: item for item in score_corpus(profile, alumni)}
        for alumnus in alumni:
            alone = score_corpus(profile, [alumnus])[0]
            item = batched[alumnus.id]
            assert item.total == alone.total
            assert item.course_overlap == alone.course_overlap
            assert item.major_match == alone.major_match
            assert item.pivot_year_alignment == alone.pivot_year_alignment
            assert item.interest_overlap == alone.interest_overlap
            assert item.shared_courses == alone.shared_courses


# --------------------------------------------------------------------------
# What the nightly job re-warms
# --------------------------------------------------------------------------


@pytest.fixture
def index(monkeypatch):
    """Stub the student's cache index with whatever the test wants in it."""

    def _set(entries: dict[str, dict] | Exception):
        async def fake(_student_id: str) -> dict[str, dict]:
            if isinstance(entries, Exception):
                raise entries
            return entries

        monkeypatch.setattr(cache, "student_index_entries", fake)

    return _set


class TestQueriesToWarm:
    async def test_bare_explore_is_always_warmed(self, index):
        index({})
        assert await queries_to_warm("stu-1", 200) == [(None, None, 200)]

    async def test_includes_the_queries_the_student_actually_ran(self, index):
        """Demand is the only honest source for this list.

        The job used to warm one entry per student while a student whose page
        always sends a pivot query missed the cache every single time.
        """
        key = cache.constellation_key("stu-1", query_hash("Economics", "Public Health", 50))
        index(
            {
                key: {
                    "kind": cache.KIND_CONSTELLATION,
                    "fromMajor": "Economics",
                    "toMajor": "Public Health",
                    "maxAlumni": 50,
                }
            }
        )
        assert await queries_to_warm("stu-1", 200) == [
            (None, None, 200),
            ("Economics", "Public Health", 50),
        ]

    async def test_skips_entries_that_are_not_constellations(self, index):
        # Timelines and combined paths are recorded so invalidation can find
        # them; they carry nothing to rebuild from.
        index(
            {
                "k1": {"kind": cache.KIND_TIMELINE},
                "k2": {"kind": cache.KIND_COMBINE},
                "k3": {},
            }
        )
        assert await queries_to_warm("stu-1", 200) == [(None, None, 200)]

    async def test_is_capped(self, index):
        index(
            {
                f"k{i}": {
                    "kind": cache.KIND_CONSTELLATION,
                    "fromMajor": None,
                    "toMajor": f"Field {i}",
                    "maxAlumni": 200,
                }
                for i in range(50)
            }
        )
        warmed = await queries_to_warm("stu-1", 200)
        assert len(warmed) == settings.precompute_max_queries_per_student

    async def test_falls_back_to_the_bare_query_when_redis_is_unreachable(self, index):
        index(ConnectionError("redis down"))
        assert await queries_to_warm("stu-1", 200) == [(None, None, 200)]
