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
(junior, CS → Product Management), and `student-other-school` (the tenant
isolation control). The seed prints a dev bearer token for each — every
student-facing route needs one, see [Auth & multi-tenancy](#auth--multi-tenancy).

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

Alumni group by **career outcome** — the industry they landed in ("Finance",
"Aerospace & Defense") — resolved through one accessor
([`app/matching/outcomes.py`](app/matching/outcomes.py)) so clustering, the
combine endpoint, the scoring destination, and the node display all agree.
Industry, not job title: "Policy Analyst", "Analyst II", and "Senior Health
Analyst" would otherwise be three clusters of one.

**Career-outcome data.** MIDFIELD stops at the degree, so employment is seeded
synthetically from the degree field ([`scripts/seed_outcomes.py`](scripts/seed_outcomes.py))
into the `career_outcomes` table, deterministically per alumnus. Every seeded
outcome is `provenance='synthetic'` and surfaces that flag in the API so the UI
never presents it as real. Alumni without an outcome fall back to the academic
`career_area`, so records (and the whole test corpus) without employment data
behave as before. Real career-center data lands as `provenance='reported'`, one
row per snapshot (`years_post_grad`), and nothing else changes.

```bash
python -m scripts.seed_outcomes --dry-run   # preview the industry mix
python -m scripts.seed_outcomes             # write synthetic outcomes (idempotent)
```

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

## Matching quality (offline eval)

This is a recommender, so the formula can't be changed by inspection. Run the
harness before and after any scoring change:

```bash
python -m app.eval --school institution-j --students 25
```

It answers three things a unit test can't:

**Do the components discriminate?** A component that returns the same value for
every alumnus consumes its weight and ranks nothing, and no per-alumnus
assertion can see it. The report flags any that were constant.

**Is the ranking better than chance?** There's no labelled "these alumni were
useful" set, and hand-building one would be circular — the career outcomes are
synthetic. So the corpus grades itself: take an alumnus who pivoted, rewind them
to their pivot (pre-pivot transcript, origin major, the year it happened), and
ask whether the engine ranks alumni who landed where they *actually* landed
above the rest. That destination is a real label from the source. The held-out
profile carries **no `intendedDirection`** — that field is scored against the
destination, so filling it in would hand the scorer the answer.

Read `lift`, not raw precision: it's precision@k over the base rate, so 1.00x
means the ranking is doing nothing a shuffle wouldn't.

**Does a component change the ordering?** Ablation drops each one and reports
rank correlation against the full model. A component can have a healthy spread
and still be inert.

Current baseline on `institution-j` (611 alumni, 25 queries):

| k | precision | recall | lift |
|---|---|---|---|
| 5 | 0.120 | 0.022 | 1.09x |
| 10 | 0.152 | 0.045 | 1.38x |
| 25 | 0.154 | 0.090 | 1.40x |

MRR 0.28. `interest_overlap` is inert on this corpus (MIDFIELD carries no
interest data), so 10% of the weight currently ranks nothing — see
[Open](#open).

## Create Path (path combining)

Students bookmark alumni journeys (`saved_paths`), then select 2+ on the Create
Path page to merge into one "ideal path". `POST /api/paths/combine`:

1. Buckets each path's non-dropped courses into the four year-stages
   (`semester_index // 2`).
2. Ranks courses within a stage by **cross-path frequency** — a course several
   paths share signals consensus — tie-broken by relevance to the student's
   interests, and capped per stage (`COMBINE_MAX_COURSES_PER_STAGE`).
3. Returns `confidence` = shared / total-unique courses, the distinct
   `outcomeFields`, the `sharedCourses`, and a **pre-computed Sankey** (one
   representative course per path per stage, one link per path per transition
   plus a link into the outcome node — the frontend just draws beziers).

Results are cached in Redis keyed by the student + the *sorted set* of alumni
combined, and tracked under the student's index so a profile change invalidates
them alongside the constellation.

Honest limits on the placeholder data: there's no prerequisite graph, so conflict
resolution reduces to de-duplicating the same course; and course identity is the
normalized code, so overlap (and thus `confidence`) only registers within an
institution — arbitrary cross-institution pairs often share nothing.

## Search

`GET /api/search?q=bioch` backs the topbar's "Search paths, majors…" box. One
flat list of uniformly-shaped rows across three kinds:

```json
{
  "query": "bioch",
  "results": [
    { "type": "major",   "id": "biochemistry",   "label": "Biochemistry",        "detail": "13 alumni", "count": 13, "provenance": null },
    { "type": "cluster", "id": "health-policy",  "label": "Health Policy",       "detail": "9 alumni",  "count": 9,  "provenance": "synthetic" },
    { "type": "alumnus", "id": "alum-42",        "label": "Class of 1990",       "detail": "Engineering → Chemical Engineering · CHE2114", "count": null, "provenance": null }
  ],
  "total": 3
}
```

Matching is `ILIKE '%q%'` ranked by pg_trgm `similarity()`, served by the GIN
trigram indexes on `alumnus_majors.name`, `alumni.career_area`, and
`alumnus_courses.course_code`. Substring rather than similarity alone because
this is a typeahead: "bioch" has to reach "Biochemistry", and a threshold loose
enough for a five-character prefix admits most of the corpus with it.

Four decisions worth stating:

* **`limit` is per type** (default 5). Course-code matches are numerous and
  score almost identically to each other, so one global cut would let a common
  prefix fill the dropdown with alumni and hide the major being typed.
* **An alumnus matches on course code only.** Majors and career areas have
  result kinds of their own; an alumnus row restating the major you just typed
  is noise beside the major row. "Who took ORGO 201" has no other answer.
* **Cluster ids are the constellation's cluster ids** — `slugify` over the same
  `outcome_industry` the map clusters on, resolved per alumnus rather than read
  off `career_area`, so a selected row names a cluster that exists. That label
  carries `provenance` when it came from seeded employment data.
* **Queries under three characters return empty.** Below that an `ILIKE '%q%'`
  contains no whole trigram and the indexes can't be used, so every keystroke
  would scan the corpus — for a prefix matching most of it.

Results are scoped to the caller's school like every other alumni read: a major,
cluster, or course code from another school never appears, since a row here
would confirm the existence of records the caller cannot read.

## Auth & multi-tenancy

Every student-facing route resolves its subject from an opaque bearer token, and
alumni reads are scoped to the caller's school.

```bash
# Register, then use the token it returns (shown once).
curl -s localhost:8000/api/students/schools
curl -s -X POST localhost:8000/api/students/register \
  -H 'Content-Type: application/json' \
  -d '{"schoolId":"demo-university","email":"you@example.edu","year":"sophomore"}'

curl -s localhost:8000/api/constellation -H "Authorization: Bearer $TOKEN"
```

Three properties are worth stating explicitly, because they're the point:

* **No route takes a student id.** `studentId` is gone from constellation,
  timeline, simulate, and paths — the subject is always the token holder. Reading
  another student's data isn't forbidden, it's unexpressible.
* **A cross-school id 404s** with the same body as an id that doesn't exist.
  Distinguishing the two would confirm which alumni a school has.
* **Tokens are stored as SHA-256 hashes.** The plaintext is transmitted once at
  registration; a database read yields nothing that can be presented as a
  credential. A lost token can only be reissued, not recovered.

Schools come from the source's institutions (MIDFIELD's Institution B/J become
`institution-b`, `institution-j`). `python -m scripts.seed` creates two schools
and prints fixed dev tokens for its sample students, so the boundary is
exercisable locally: `student-other-school`'s token must 404 on every `alum-*`
id the others can read.

`ADMIN_API_KEY` gates `/api/admin` as `X-Admin-Key`. **Unset means disabled**
(503 for everyone), not open.

### What this is not

This is the enforcement boundary, not an identity provider. Before real students
touch it:

* **Registration is open to anyone naming a valid school.** It needs invite
  codes, a domain allowlist, or campus SSO in front of it. The token issuance
  here is the shape an OIDC callback would mint into.
* **Bearer tokens assume TLS.** They're sent in a header on every request and
  are as good as the transport.
* **Tokens never expire and there is no revocation endpoint.** Rotation means
  issuing a new hash out of band.
* Registration has no rate limiting, so it's an unmetered write.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/students/schools` | — | Schools available at registration |
| `POST` | `/api/students/register` | — | Sign up (school + password), receive a token |
| `POST` | `/api/students/login` | — | Exchange email + password for a fresh token |
| `GET` | `/api/students/me` | student | The caller's profile |
| `GET` | `/api/students/me/activity` | student | Recent activity feed |
| `PUT` | `/api/students/me` | student | Partial profile update |
| `PUT` | `/api/students/me/courses` | student | Replace the transcript |
| `GET` | `/api/students/me/dashboard` | student | Dashboard stats + top matches |
| `GET` | `/api/constellation` | student | Full constellation payload |
| `GET` | `/api/alumni/{id}/timeline` | student | Lazily-fetched detail panel |
| `POST` | `/api/simulate` | student | What If Simulator — aggregates + top 5 cards |
| `GET` | `/api/paths` | student | Saved paths, with full timelines |
| `POST` | `/api/paths` | student | Bookmark an alumnus path (idempotent) |
| `DELETE` | `/api/paths/{id}` | student | Remove a saved path |
| `POST` | `/api/paths/combine` | student | Merge 2+ saved paths into one plan |
| `GET` | `/api/search?q=` | student | Typeahead across majors, clusters, and alumni |
| `GET` | `/health/ready` | — | Postgres + Redis status |
| `POST` | `/api/admin/recompute` | admin | Rebuild all cached constellations |
| `POST` | `/api/admin/recompute/{studentId}` | admin | Rebuild or invalidate one student |
| `DELETE` | `/api/admin/cache` | admin | Flush cached constellations |

`GET /api/students/me/dashboard` is a projection of the caller's constellation —
the same cached entry `/api/constellation` serves — plus a live count of their
saved paths, so a stat card can't disagree with the map it links to. Note what
it does *not* answer: the design's "Clusters Explored" card means clusters the
student has **visited**, which would need an activity log this service doesn't
keep. It returns the number of clusters in the constellation being explored and
says so in the field description, rather than inventing a visit count.

`GET /api/constellation` also accepts:

| Param | Meaning |
|---|---|
| `interests` | Comma-separated. Matches on **any** of them |
| `careerArea` | Where they ended up — industry or academic area, never a major |
| `major` | Either end of the path: started in it, or graduated in it |
| `fromMajor` / `toMajor` | Pivot query, for the What If view |
| `maxAlumni` | Cap on returned alumni (default 200) |
| `refresh=true` | Bypass the cache and recompute |

Facets **filter, they don't score**: they decide who is eligible, and the
weighted formula ranks whoever survives. Every one of them is part of the cache
key — a facet left out would write a filtered result over the unfiltered entry.

Alumni are nested inside the cluster they belong to, since the grouping is the
layout, and `similarityScore` is 0–100 (the frontend renders it as a match
percentage).

**The response contains no geometry.** Radius, angle, and coordinates are the
frontend's concern; shipping them would freeze its ability to change the layout
model. A test asserts this.

Each alumnus carries a `programs` list — majors and minors with `role` and
`provenance` — alongside the flat `majors` names; `provenance='derived'` marks an
inferred minor. Simulate matches include a `pivotType` (`added`/`dropped`/
`switched`).

**`matchReason`** is a one-line rationale — "Shared Genetics and Organic Chem,
started in Biochemistry, like you" — on every node, with the clauses also
available unjoined on the detail payload. Two rules make it trustworthy:

* **Generated from the score components, never narrated.** It is derived from the
  same numbers that produced the ranking, so it cannot disagree with the order
  the nodes are in. A model asked to explain a ranking after the fact writes
  fluent reasons for an order it never saw, and the first divergence teaches the
  student to distrust the map.
* **It describes the overlap, never the destination.** Career outcomes are
  `provenance='synthetic'` on the placeholder dataset, and a rationale is exactly
  where fabricated data reads as fact. The destination is already in the payload
  beside its provenance flag.

It is `null` when nothing specific matched — an alumnus can rank on neutral
defaults alone, and inventing a reason for those is the failure the feature is
shaped to avoid.

Timeline course status is resolved relative to the viewing student — `kept` is a
course you've already taken, `added` is one you haven't, `dropped` is one the
alumnus abandoned. That framing is what makes the timeline answer "what changes
if I follow this path" rather than just "what did this person do". The student is
the authenticated caller, so the comparison is always present.

## Layout

```
app/
  main.py            FastAPI app, CORS, router registration
  config.py          Settings from environment
  db.py              Async engine and session factory
  cache.py           Redis keys, TTLs, gzip storage, invalidation
  api/responses.py   Cached-response encoding, ETag/304
  repository.py      Queries, with relationships eager-loaded
  search.py          Typeahead row shaping and ordering (pure)
  models/            SQLAlchemy ORM
  schemas/           Pydantic — camelCase on the wire
  matching/
    text.py          Normalization, Jaccard, fuzzy label matching
    programs.py      Major/minor accessors + set-diff pivots (the one seam)
    explain.py       matchReason, derived from the score components
    outcomes.py      Career-outcome accessor (the clustering axis)
    corpus.py        Per-school prepared corpus (student-independent derivations)
  facets.py        Explore filters — interests, career area, major
    scoring.py       The weighted formula (NumPy-vectorized overlap)
    clustering.py    Career-outcome grouping and edge pruning
    timeline.py      Semester timeline for the detail panel
    combine.py       Path combining — merge saved paths + Sankey
  transitions.py   What-If cards and the aggregates above them
    pipeline.py      score → rank → cut → cluster → prune
  jobs/
    recompute.py     Background precompute (one corpus per school)
    derive_minors.py Infer de facto minors from course concentration
  eval/              Offline matching-quality harness
    metrics.py       Ranking + distribution metrics (pure)
    harness.py       Held-out pivot prediction, ablation, distributions
    report.py        Text rendering
  ingest/            Swappable data-source layer
    records.py       Source-neutral dataclasses (the adapter contract)
    loader.py        Records → Postgres (the only ORM-facing code)
    cip.py           CIP code → major name / career area
    sources/         midfield.py, template.py, registry
  auth.py            Bearer tokens, current_student, admin gate
  api/routes/        students, dashboard, constellation, alumni, search,
                     simulate, paths, admin, health
scripts/
  seed.py            Synthetic corpus generator (fixed RNG seed)
  seed_outcomes.py   Synthetic employment outcomes (the clustering axis)
tests/               385 tests; only the security suite needs Postgres
```

The matching engine takes plain ORM objects and never touches a session, so
everything but `test_api_security.py` runs without Postgres — and that file
skips cleanly when the database isn't up.

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
- A student's school is set at registration and cannot be changed through the
  API — moving between tenants would hand over another school's corpus.
- `semester_index` (0–7) is the source of truth for timing; labels like
  "Sophomore Spring" are derived. Pre-pivot filtering and year alignment are
  then integer comparisons rather than string parsing.
- The seed corpus is generated from a fixed RNG seed, so scoring changes show up
  as reviewable diffs rather than noise.

### Open

- **Similarity range.** Scores span roughly 0.05–0.85 on real data. Part of the
  original "narrow spread" complaint was a bug (major-match was a constant; see
  the program key-space fix), and part is structural: a component with no data
  behind it contributes a hard 0 rather than being renormalized away, which
  compresses every score. `interest_overlap` does exactly that on MIDFIELD —
  10% of the weight is inert, confirmed by `python -m app.eval`. Renormalizing
  over the components a source actually populates is the fix.
- **Thin transcripts quantize the primary signal.** Course overlap divides by
  the student's course count, so a 6-course student's 50% component takes only
  7 possible values across the whole corpus — huge ties, broken arbitrarily on
  id. It hits early-year students hardest, who have the least else to rank on.
  Needs smoothing or a confidence weight, not a bigger corpus.
- **Pivot query filter.** `filter_by_pivot_query` requires both origin and
  destination to clear 0.3 similarity, which is tight — a Biochemistry →
  Health Policy query narrows 240 alumni to 5. Worth loosening once there's
  real data to tune against.
- **Thin constellations.** A focused query returning 3 alumni in 1 cluster is
  served as-is. The frontend spec notes the constellation is the wrong shape for
  that and may need a list fallback.
