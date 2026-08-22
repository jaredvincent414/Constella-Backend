# Constella

Finds people who already graduated from a student's school, works out which of
them started where that student is standing now, and shows what they did next.
The frontend draws it as a star map — each point a former graduate, clustered by
where people ended up.

| The student asks | The engine answers |
|---|---|
| *Who took the classes I'm taking?* | Graduates ranked by early-coursework overlap |
| *What if I switched Economics to Public Health?* | Everyone who made that switch, when they did it, and what they took |
| *What would my next two years look like?* | Real paths merged into one plan |

## The problem

Students pick majors, change direction, or graduate — all without seeing what
people like them actually did. Advisors carry anecdotes; registrars carry data
but no tools to surface it. The result: students make high-stakes decisions
with incomplete information.

Constella turns a school's own alumni records into a navigable map of real
academic paths. Every match is a graduate who started where the student stands
now — same early courses, same starting major — and shows where they ended up
and how they got there.

## How it works

```
Postgres (alumni + student data)
    |
Background job (scores + clusters)
    |
Redis (precomputed constellations)
    |
FastAPI (serves results, ~5ms cached)
    |
Frontend (renders star map)
```

The expensive part — scoring one student against every alumnus — runs overnight.
Loading a map reads a prepared answer: ~5ms instead of ~600ms. On a cache miss
the API computes inline, so a cold Redis costs latency, not availability.

### Scoring

Half the score is shared coursework — only classes taken *before* the alumnus
changed direction, since what they studied afterward says little about someone
standing at the fork.

| Factor | Weight | What it measures |
|---|---|---|
| Course overlap | 50% | Shared pre-pivot courses / student's total courses |
| Pivot timing | 20% | How close the alumnus pivoted to where the student is now |
| Major match | 20% | Weighted Jaccard over major and minor sets |
| Interests | 10% | Jaccard over interest tokens |

Filters decide *who is eligible*; the formula only ranks the survivors — a
strong course overlap can't outvote an explicit "show me Health Policy."

### Clustering

Alumni group by career outcome (industry, not job title). "Policy Analyst" and
"Senior Health Analyst" land in the same cluster. Cluster similarity is the
mean of member scores, capped at 10 clusters.

## School data integration

Schools connect their alumni data through standard higher-ed infrastructure —
no CSV uploads, no manual entry.

### Privacy pipeline

PESC College Transcript XML contains names, DOBs, SSNs. Constella processes
this without persisting any of it. The architecture enforces this structurally,
not by policy:

```
INGESTION BOUNDARY
  PESC XML arrives via POST endpoint
          |
PII EXTRACTION ZONE (in-memory only)
  1. Stream-parse XML (bounded memory)
  2. Per student:
     a. Name + DOB --> HMAC-SHA256 anonymous ID (server-side secret)
     b. Extract courses, majors, pivots
     c. DISCARD all personal fields
  3. Yield anonymous academic records only
          |
AUDIT GENERATION
  Per-record audit entry:
  - fields_received, fields_retained, fields_discarded
  - validation_flags, record_status
  Schools see exactly what happened without seeing PII.
          |
PERSISTENCE BOUNDARY
  Only anonymous academic records cross this line.
  Alumni have no name column by construction.
```

| PII field | Used for | Persisted |
|---|---|---|
| Name, DOB | Anonymous hash input | No |
| SSN, address, phone, email | Nothing | No |

| Academic field | Persisted as | Why |
|---|---|---|
| Course codes + names | `AlumnusCourse` | Core of matching engine |
| CIP codes, programs | `AlumnusMajor` | Major classification, pivot detection |
| Session dates | `semester_index` (0-7) | Timeline construction |
| Award dates | `graduation_year` | Cohort filtering |

### Integration modes

**Phase 1 — PESC push.** Schools push College Transcript XML to a per-school
authenticated endpoint. The adapter maps PESC fields to Constella's
source-neutral records; the loader, scorer, and API work unchanged.

**Phase 2 — NSC pull.** Schools authorize Constella as a data recipient in
NSC's system. The adapter authenticates with NSC and pulls via ETX or NextGen,
reusing the same PESC parsing utilities.

**Phase 3 — Career outcomes.** Schools push employment and career survey data,
replacing synthetic outcomes with `provenance='reported'`.

### Admin portal

School administrators get a dedicated dashboard:

- **Sync history** — status, record counts, timestamps for every data sync
- **Per-record audit** — what fields were received, what was kept, what was discarded
- **Privacy compliance** — which PII categories were encountered, confirmation all were discarded
- **Corpus overview** — alumni count, major/career distributions, data completeness
- **Token rotation** — rotate the per-school ingestion token without downtime

Auth reuses the existing token-hash pattern (no JWT, no new dependencies).
Admin users are school-scoped — an admin sees only their own school's data.

## Key features

**Explore** — the constellation map. Alumni matched by coursework overlap,
major alignment, and pivot timing, clustered by career outcome. Filterable by
interests, career area, and major. Typeahead search across majors, clusters,
and alumni backed by GIN trigram indexes.

**Transition** — "What if I switched from Biology to Computer Science?"
Returns every alumnus who made that transition: when they pivoted, what courses
they took around the pivot, and where they ended up. Aggregate stats
(`peakTiming`, `topOutcome`) are computed over the full match set, not just the
displayed cards.

**Create Path** — bookmark alumni journeys, then merge 2+ into one plan.
Courses are ranked by cross-path frequency (consensus) and capped per stage.
Match rationales are derived from the same score components that produced the
ranking, never narrated by a model — a rationale that disagrees with the
ordering is worse than none.

## Security

- No student-facing route accepts a student ID — the subject is always the bearer-token holder
- Alumni reads are school-scoped; a cross-school ID returns 404 (not 403)
- Tokens stored as SHA-256 hashes; plaintext returned once at registration
- Login is not an enumeration oracle — same response for wrong password, unknown email, and no-password accounts
- Failed logins throttled per email and per IP
- `ADMIN_API_KEY` unset means disabled (503), not open
- A student's school is immutable through the API

## Matching quality

The engine is evaluated against held-out alumni pivots — real labels from the
source data, not synthetic outcomes:

| k | precision | lift |
|---|---|---|
| 5 | 0.120 | 1.09x |
| 10 | 0.152 | 1.38x |
| 25 | 0.154 | 1.40x |

~1.4x better than chance. Honest about the gap — 10% of score weight
(interests) is inert on the current dataset. The eval harness catches
components that rank nothing and components that vary but don't reorder.

## Quickstart

```bash
docker compose up -d
uv sync --all-groups && source .venv/bin/activate
cp .env.example .env
alembic upgrade head
python -m scripts.seed --alumni 240 --reset
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000/docs for the interactive API.

```bash
pytest                              # 399 tests
ruff check app scripts tests        # linting
python -m app.eval --school institution-j  # matching quality
```

## Demo notes

- **Career outcomes are synthetic.** Transcripts are real (~1,200 anonymized
  records); employment data is generated and tagged `provenance='synthetic'`.
- **Registration is open.** No invites or university login — demo only.
- **Matching is ~1.4x better than chance**, measured. A real signal, not a
  solved problem.

## Technical details

Full architecture notes, scoring formula details, security invariants, and
development conventions are documented in [CLAUDE.md](CLAUDE.md).

School data integration architecture is detailed in
[docs/admin-portal-backend.md](docs/admin-portal-backend.md).

Frontend API migration guide is in
[docs/frontend-api-update-handoff.md](docs/frontend-api-update-handoff.md).
