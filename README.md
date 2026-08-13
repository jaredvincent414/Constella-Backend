# Constella — Backend

Cohort Matching Engine for the constellation map. Scores alumni against a
student's transcript, clusters them by career outcome, and serves the payload
the frontend renders.

## Quickstart

```bash
docker compose up -d
uv sync --all-groups
source .venv/bin/activate
cp .env.example .env
alembic upgrade head
python -m scripts.seed --alumni 240 --reset
uvicorn app.main:app --reload --port 8000
```

Interactive API docs at http://localhost:8000/docs.

Seeded students: `student-demo` (sophomore, Biochemistry → Health Policy),
`student-undeclared` (freshman, thin transcript), `student-junior-cs`
(junior, CS → Product Management).

Instead of the synthetic seed, you can load real practice data from MIDFIELD —
see [Data ingestion](#data-ingestion).

## Architecture

```
PostgreSQL (raw alumni + student data)
    ↓
Background job (scores + clusters)          app/jobs/recompute.py
    ↓
Redis (scored alumni, cluster assignments)  app/cache.py
    ↓
FastAPI (serves precomputed results)        app/api/routes/
    ↓
Frontend (renders constellation)
```

Jobs re-run when new alumni land or a student's profile changes — not on every
page load. On a cache miss the API computes inline and backfills, so a cold or
unavailable Redis costs latency, not availability. `/health/ready` reports that
state as `degraded: true`.

## Data ingestion

The corpus is loaded through a **swappable source adapter**, so the practice
dataset can be replaced with real school data by writing one class — nothing
downstream changes.

```
source adapter (dataset-specific)  →  source-neutral records  →  loader  →  Postgres
   sources/midfield.py                    records.py             loader.py     (ORM)
```

The matching engine, cache, and API only read the ORM tables the loader fills;
they never learn which dataset produced them. A source implements two methods
(`alumni()`, `students()`) that yield the dataclasses in `app/ingest/records.py`.

The default source is **MIDFIELD** — anonymized registrar records for ~98k
students across three institutions. Its adapter derives pivots from program
(CIP) changes across terms, dropped courses from `W` grades, majors from the CIP
sequence plus the awarded degree, and — since MIDFIELD carries no employment
data — clusters on **academic discipline** as an honest stand-in for career
outcome (`outcome_title`/`outcome_org` are the degree and institution, not a
job). Interests are absent, so the 10% interest component contributes nothing.

```bash
python -m app.ingest --source midfield --dry-run --alumni 300   # preview, no DB write
python -m app.ingest --source midfield --alumni 1500 --reset    # load into Postgres
python -m app.jobs.recompute                                    # rebuild cached constellations
```

`--alumni 0` loads the full ~50k-degree corpus. Defaults come from the
environment (`DATA_SOURCE`, `DATA_PATH`, `INGEST_ALUMNI_LIMIT`,
`INGEST_STUDENT_COUNT`, `INGEST_SEED`). The loader also synthesizes a few
"current students" from real pre-pivot transcripts (`mid-student-*`) so the
matching pipeline has something to score against out of the box.

**Swapping in real data:** copy `app/ingest/sources/template.py`, implement the
two methods against the school's export (filling `career_area` /
`outcome_title` / `outcome_org` with real employment data — clustering then
becomes true career-outcome clustering), add one line to the registry in
`app/ingest/sources/__init__.py`, and set `DATA_SOURCE` / `DATA_PATH`. The
loader, models, scorer, clustering, cache, and API are unchanged. Full mapping
table, caveats, and checklist in [app/ingest/README.md](app/ingest/README.md).

## Scoring

| Factor | Weight | Implementation |
|---|---|---|
| Course overlap | 50% | `shared / total_student_courses`, over the alumnus's **pre-pivot** transcript |
| Pivot year alignment | 20% | `1 - |pivot_year - student_year| / 3` |
| From/To major match | 20% | Half origin, half destination; **weighted Jaccard over major *sets*** |
| Interest overlap | 10% | Jaccard over interest tokens |

Course overlap is **directional** — it divides by the student's course count, so
an alumnus with 40 courses sharing 8 with you scores the same as one with 15
sharing 8.

Major match is **set-based**: a student or alumnus may hold several majors, and
`CS+Art` vs `CS-only` scores partial, not 0 or 1. The comparison is a *soft*
weighted Jaccard whose element kernel is fuzzy name similarity, so a single-major
record scores exactly as it did before sets existed. Minors participate at a
reduced weight (`MINOR_MATCH_WEIGHT`). See [Majors & minors](#majors--minors).

Two cases score *neutral* (0.5) rather than zero, because absence of information
isn't evidence of a mismatch: an alumnus who never pivoted has no pivot year to
align against, and an undeclared student has no major to match.

Scoring is deterministic and ties break on id — the same query always produces
the same order, which the frontend's spatial memory depends on.

### Majors & minors

A person's programs live in a role-tagged join table (`alumnus_majors` /
`student_program`), never a scalar — one row per program per term, with
`role` (`primary` | `second_major` | `minor` | `concentration`) and `provenance`
(`reported` | `derived`). Multiple minors are just multiple `role='minor'` rows.
This is what lets the placeholder dataset (one major per student) be swapped for
real registrar data (primary + second majors + minors) with **no schema change
and no change to the scoring code**.

Every read goes through one accessor module, [`app/matching/programs.py`](app/matching/programs.py):
`majors_at(entity, term)` / `minors_at(entity, term)` return **sets** (never a
bare string, even for one major); `detect_pivots(entity)` computes pivots as a
**set difference** between consecutive terms, tagged `added` / `dropped` /
`switched` so the What-If Simulator can tell "added a minor" from "switched
majors". A test asserts no other module reads the program field directly.

**Derived minors.** The placeholder data has no minors, so a separate offline
job infers them from course concentration — a run of coursework in a discipline
outside a student's declared field (thresholds `DERIVED_MINOR_MIN_COURSES` /
`_MIN_CREDITS`). These rows are `provenance='derived'` and surface in the API
under each alumnus's `programs` list so the UI can label them **inferred**, never
as formally declared.

```bash
python -m app.jobs.derive_minors --dry-run   # report the per-student distribution
python -m app.jobs.derive_minors             # write derived minors (idempotent)
```

### Clustering

Alumni group by **career area**, not job title: "Policy Analyst", "Analyst II",
and "Senior Health Analyst" would otherwise be three clusters of one.

Cluster similarity is the **mean** of member scores, not the max. Radius is drawn
from this number on the frontend, so a cluster with one strong member and eleven
weak ones must not appear close.

Clusters are capped at 10 — past that the radial layout's angular sectors get
too thin to label.

### Edge pruning

`clusterEdges` is Jaccard over the clusters' major sets, filtered to
`weight >= 0.25` and capped at the top 12. These match the frontend's render
rules exactly so the API isn't shipping edges that get discarded. Both bounds
are configurable (`EDGE_MIN_WEIGHT`, `EDGE_MAX_COUNT`).

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/constellation?studentId=` | Full constellation payload |
| `GET` | `/api/alumni/{id}/timeline?studentId=` | Lazily-fetched detail panel |
| `POST` | `/api/simulate` | What If Simulator — top 5 matches |
| `GET` | `/health/ready` | Postgres + Redis status |
| `POST` | `/api/admin/recompute` | Rebuild all cached constellations |
| `POST` | `/api/admin/recompute/{studentId}` | Rebuild or invalidate one student |
| `DELETE` | `/api/admin/cache` | Flush cached constellations |

`GET /api/constellation` also accepts `fromMajor`, `toMajor`, `maxAlumni`, and
`refresh=true` (bypass cache).

**The response contains no geometry.** Radius, angle, and coordinates are the
frontend's concern; shipping them would freeze its ability to change the layout
model. A test asserts this.

Each alumnus carries a `programs` list — majors and minors with `role` and
`provenance` — alongside the flat `majors` names; `provenance='derived'` marks an
inferred minor. Simulate matches include a `pivotType` (`added`/`dropped`/
`switched`).

Timeline course status is resolved relative to the viewing student — `kept` is a
course you've already taken, `added` is one you haven't, `dropped` is one the
alumnus abandoned. That framing is what makes the timeline answer "what changes
if I follow this path" rather than just "what did this person do". Omit
`studentId` and nothing is `added`.

The admin routes are unauthenticated for local development. Put them behind auth
before exposing this anywhere real.

## Layout

```
app/
  main.py            FastAPI app, CORS, router registration
  config.py          Settings from environment
  db.py              Async engine and session factory
  cache.py           Redis keys, TTLs, invalidation
  repository.py      Queries, with relationships eager-loaded
  models/            SQLAlchemy ORM
  schemas/           Pydantic — camelCase on the wire
  matching/
    text.py          Normalization, Jaccard, fuzzy label matching
    programs.py      Major/minor accessors + set-diff pivots (the one seam)
    scoring.py       The weighted formula (NumPy-vectorized overlap)
    clustering.py    Career-area grouping and edge pruning
    timeline.py      Semester timeline for the detail panel
    pipeline.py      score → rank → cut → cluster → prune
  jobs/
    recompute.py     Background precompute
    derive_minors.py Infer de facto minors from course concentration
  ingest/            Swappable data-source layer
    records.py       Source-neutral dataclasses (the adapter contract)
    loader.py        Records → Postgres (the only ORM-facing code)
    cip.py           CIP code → major name / career area
    sources/         midfield.py, template.py, registry
  api/routes/        constellation, alumni, simulate, admin, health
scripts/seed.py      Synthetic corpus generator (fixed RNG seed)
tests/               58 tests, no database required
```

The matching engine takes plain ORM objects and never touches a session, so the
whole test suite runs without Postgres.

## Development

```bash
pytest
ruff check app scripts tests
alembic revision --autogenerate -m "message"
```

Compose maps Postgres to **5433** and Redis to **6380** so the stack won't
collide with anything already running locally.

## Notes

- Alumni have no name column. The frontend renders "Class of 2022", so the API
  has nothing to leak.
- `semester_index` (0–7) is the source of truth for timing; labels like
  "Sophomore Spring" are derived. Pre-pivot filtering and year alignment are
  then integer comparisons rather than string parsing.
- The seed corpus is generated from a fixed RNG seed, so scoring changes show up
  as reviewable diffs rather than noise.

### Open

- **Similarity range.** Scores cluster in roughly 0.35–0.70 on the synthetic
  corpus. The frontend spec flags that a narrow spread makes the radial layout
  visually flat; whether that needs a domain stretch depends on real score
  distributions, not these.
- **Pivot query filter.** `filter_by_pivot_query` requires both origin and
  destination to clear 0.3 similarity, which is tight — a Biochemistry →
  Health Policy query narrows 240 alumni to 5. Worth loosening once there's
  real data to tune against.
- **Thin constellations.** A focused query returning 3 alumni in 1 cluster is
  served as-is. The frontend spec notes the constellation is the wrong shape for
  that and may need a list fallback.
