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

## Scoring

| Factor | Weight | Implementation |
|---|---|---|
| Course overlap | 50% | `shared / total_student_courses`, over the alumnus's **pre-pivot** transcript |
| Pivot year alignment | 20% | `1 - |pivot_year - student_year| / 3` |
| From/To major match | 20% | Half origin, half destination; token-set similarity |
| Interest overlap | 10% | Jaccard over interest tokens |

Course overlap is **directional** — it divides by the student's course count, so
an alumnus with 40 courses sharing 8 with you scores the same as one with 15
sharing 8.

Two cases score *neutral* (0.5) rather than zero, because absence of information
isn't evidence of a mismatch: an alumnus who never pivoted has no pivot year to
align against, and an undeclared student has no major to match.

Scoring is deterministic and ties break on id — the same query always produces
the same order, which the frontend's spatial memory depends on.

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
    scoring.py       The weighted formula (NumPy-vectorized overlap)
    clustering.py    Career-area grouping and edge pruning
    timeline.py      Semester timeline for the detail panel
    pipeline.py      score → rank → cut → cluster → prune
  jobs/recompute.py  Background precompute
  api/routes/        constellation, alumni, simulate, admin, health
scripts/seed.py      Synthetic corpus generator (fixed RNG seed)
tests/               44 tests, no database required
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
