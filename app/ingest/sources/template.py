"""Template for a new data source — copy this when real school data arrives.

The contract is small on purpose. Read whatever the school sends (CSV dump, a
registrar API, a warehouse query) and emit the same `AlumnusRecord` /
`StudentRecord` shapes the MIDFIELD adapter emits. Everything downstream — the
loader, scorer, clustering, cache, and API — is already written against those
records and needs no changes.

Steps to go live on real data:
    1. Copy this file to e.g. `sources/my_university.py` and implement the two
       methods below.
    2. Register it in `sources/__init__.py`: add `MyUniversityAdapter.name:
       MyUniversityAdapter` to `_REGISTRY`.
    3. Set `DATA_SOURCE=my-university` (and `DATA_PATH=...`) in the environment,
       or pass `--source my-university` to `python -m app.ingest`.
    4. Run the load, then recompute: `POST /api/admin/recompute`.

Fill real values into `career_area`, `outcome_title`, and `outcome_org` — with
genuine employment data the clustering becomes career-outcome clustering as the
spec intends, instead of MIDFIELD's academic-discipline stand-in.

This module is intentionally NOT registered; it's a reference skeleton.
"""

from __future__ import annotations

from collections.abc import Iterator

from app.ingest.records import AlumnusRecord, CourseRecord, StudentRecord


class TemplateAdapter:
    name = "template"

    def __init__(self, path: str, *, alumni_limit: int = 0, student_count: int = 0, seed: int = 0):
        self.path = path
        self.alumni_limit = alumni_limit
        self.student_count = student_count
        self.seed = seed

    def alumni(self) -> Iterator[AlumnusRecord]:
        # Replace with real reads. One illustrative record:
        yield AlumnusRecord(
            id="example-1",
            graduation_year=2022,
            outcome_title="Software Engineer",          # real job title
            outcome_org="Example Corp",                 # real employer
            career_area="Software Engineering",          # the clustering axis
            interests=["Robotics Club"],
            courses=[CourseRecord(code="CS201", name="Data Structures", semester_index=2)],
            majors=[],
            pivots=[],
            milestones=[],
        )

    def students(self) -> Iterator[StudentRecord]:
        return iter(())  # a source may legitimately provide no current students
