# CLAUDE.md

Guidance for Claude Code working in this repository.

## What this is

Constella's backend: a cohort-matching engine. It scores alumni against a
current student's transcript, clusters them by career outcome, and serves the
payload the frontend renders as a constellation map. FastAPI + async SQLAlchemy
+ Postgres + Redis.

Read [README.md](README.md) first for the domain model and the scoring formula.
This file covers what isn't obvious from the code.

## Commands

```bash
docker compose up -d                      # Postgres on 5433, Redis on 6380
uv sync --all-groups && source .venv/bin/activate
alembic upgrade head
python -m scripts.seed --alumni 240 --reset   # synthetic corpus + dev tokens
uvicorn app.main:app --reload --port 8000
```

```bash
pytest                                    # 266 tests
pytest tests/test_api_security.py         # needs a migrated Postgres; skips without one
ruff check app scripts tests              # line-length 100
alembic revision --autogenerate -m "msg"
```

Real practice data instead of the seed:

```bash
python -m app.ingest --source midfield --alumni 1500 --reset
python -m app.jobs.recompute
```

Before and after **any** change to the scoring formula:

```bash
python -m app.eval --school institution-j
```

## Security invariants

These are load-bearing. Changing any of them is a security change, not a
refactor — say so explicitly in the PR.

1. **No student-facing route accepts a student id.** The subject of every
   request is the bearer-token holder, resolved by `current_student`
   ([app/auth.py](app/auth.py)). If you find yourself adding a `studentId`
   parameter, you are re-opening the hole this design closed: it made reading
   another student's data a matter of guessing an id. `/me` is the addressing
   scheme.

2. **Alumni reads are school-scoped.** `repository.list_alumni` and
   `get_alumnus` take a `school_id`; every request-path caller passes
   `student.school_id`. A cross-school id must 404 with the same body as a
   nonexistent one — never 403, and never a different message, because that
   confirms the record exists.

   The one legitimate unscoped caller is `app/jobs/derive_minors.py`, an offline
   enrichment job that walks the whole corpus and serves no request.

   Note the sharp edge: `school_id=None` means *unscoped*, not "no matches". So
   `current_student` refuses to authenticate a student whose `school_id` is null
   — otherwise a null tenant would fail **open** onto every school's corpus.
   Keep that check if you rework auth.

3. **The precompute job scopes exactly as the request path does.** A job that
   scored against all schools would write a cross-tenant constellation into
   Redis, and the route would serve it on the next cache hit without ever
   touching the corpus. Isolation has to hold on both sides of the cache.

4. **Tokens are stored only as SHA-256 hashes.** Plaintext is returned once, at
   registration. Never log a token, never add an endpoint that returns one for
   an existing student, and never make the hash acceptable as a credential.

5. **`ADMIN_API_KEY` unset means disabled (503), not open.** The admin gate is a
   router-level dependency on `/api/admin`, so new endpoints added there are
   protected by default. Keep it that way rather than gating per-route.

6. **A student's school is immutable through the API.** `ProfileUpdate` has no
   `schoolId` field on purpose.

The security boundary is covered by [tests/test_api_security.py](tests/test_api_security.py)
(integration, DB-guarded) and [tests/test_auth.py](tests/test_auth.py) (pure
unit). If you touch auth or scoping, those tests must still pass and probably
need a new case.

### Known gaps — deliberate, not oversights

Registration is open to anyone naming a valid school (needs invites/SSO in
front of it); bearer tokens assume TLS; tokens don't expire and there's no
revocation endpoint; registration isn't rate-limited. These are documented in
the README's "What this is not". Don't quietly close one halfway — either do it
properly or leave the honest note.

## Architecture notes

**A cached read touches Postgres for nothing.** `current_principal`
([app/auth.py](app/auth.py)) resolves a token to `(student_id, school_id)` —
all the tenant boundary needs — and caches it, and the cached routes take no
session dependency, because FastAPI resolves dependencies eagerly and one would
check out a connection on every hit. Verified: ten constellation hits, ten
timeline hits, and ten 304s all issue **0 SQL statements**. That is an
availability property as much as a latency one; keep it when adding routes.

`current_student` is the uncached form and still loads a live ORM instance —
use it for anything that reads a full profile or writes one. Never cache one:
a reconstituted instance is detached and unsafe to write through.

**The auth cache is not revocation.** `auth_cache_ttl_seconds` (60s) is the
window in which a deleted student can still authenticate. `set_principal`
refuses to store a null tenant, so the fail-closed 403 can't be cached into a
fail-open one. DB-backed tests that reuse a fixed token must clear it —
`tests/test_api_security.py` does this in `_cleanup`, or a principal outlives
the row it names.

**`pool_pre_ping` is off.** It costs a `SELECT 1` per connection checkout (~0.7ms
measured on every request that touches Postgres); `pool_recycle` covers the
idled-out-connection case it usually guards. Turn it back on behind a proxy or
failover pair, where connections drop unannounced.

**Cached responses carry an ETag and revalidate.** `private, no-cache` means
"store it, always check" — so a returning client gets a 304 with no body
instead of the payload, and never sees something stale. The ETag includes the
encoding, because a gzip body and an identity body are different entities. Only
the *cached* path sets one; a miss returns the freshly built model, whose
`meta.cached` is false.

**The pipeline.** `Postgres → background job (score + cluster) → Redis →
FastAPI`. On a cache miss the API computes inline and backfills, so a cold or
unavailable Redis costs latency, not availability. Cache writes are always
wrapped in `try/except` — caching is an optimization and must never fail a
request.

**`CACHE_TTL_SECONDS` must exceed the gap between recompute runs.** A TTL
shorter than the job period is the worst case available: entries expire long
before the job refreshes them, so the cache sits cold for most of the day and
nearly every request pays a full inline recompute. The default (30h) assumes a
daily job.

**A cache hit returns the stored bytes.** Entries are written already stamped
`meta.cached: true` (`cache.serialize_cached`) and served with
`Response(content=raw)`, so a hit never parses, revalidates, and re-serializes
a payload it already had. If you change how a response is encoded, change the
serializer too — the point is that the cached copy is byte-identical to what
the route would have computed.

**Entries are stored gzipped and served gzipped.** A constellation is ~97% a
repetitive array of alumni records: 78.8 KB becomes 4.1 KB, so the Redis working
set and the bytes on the wire both drop ~19x, and the read path still doesn't
touch the payload — the browser's `Accept-Encoding: gzip` means the stored bytes
*are* the response. `app/api/responses.py` decompresses only for a client that
says it can't take gzip. The Redis client is deliberately `decode_responses=False`
for this reason; the index reads decode explicitly.

Don't add Starlette's `GZipMiddleware` on top: as of 1.6.0 it does not check for
an existing `Content-Encoding` and would compress these responses a second time.

The rejected alternative was splitting the payload into a per-school alumnus
dictionary plus a thin per-student list. 75% of a constellation is
student-independent, so that saves ~4x — but it costs a second round trip and a
join on every hit, to undo the property above.

**Bumping `CACHE_VERSION` orphans the old keyspace, it doesn't free it.** Those
entries stay resident until their TTL, which is now days. `invalidate_all_constellations`
is deliberately version-agnostic (`FLUSH_PATTERN = "constella:*"`) so
`DELETE /api/admin/cache` reclaims them — run it after a deploy that bumps the
version. A v6 keyspace measured 3.6 MB against 10 KB of live v7 entries.

**The per-student index is a hash of key → the query behind it**, not a set of
keys. That is what lets the nightly job re-warm the queries a student actually
ran instead of only the bare explore. `build_constellation_for_query` is shared
by the route and the job on purpose: both derive the same cache key from
(fromMajor, toMajor, limit), so they have to agree on what that key *means*.

**The recompute job overwrites in place**, then clears what it didn't rewrite.
Don't reintroduce a flush at the top of `precompute_all` — it makes every
student cold for the length of the run and buys nothing.

**The corpus is prepared once per school, not once per student.**
[app/matching/corpus.py](app/matching/corpus.py) precomputes everything the
scorer derives from an alumnus that doesn't depend on the student — normalized
pre-pivot course codes, origin/final majors, minors, interest tokens, pivot
year. `score_corpus` and `build_constellation` still accept a plain list and
prepare one themselves, so single-student callers are unchanged; the nightly job
builds one `Corpus` per school and reuses it. This took the job from ~549ms to
~16ms per student.

**`score_corpus(..., top_n=N)` cuts before materializing, not after.** Ranking
needs only the total; the rounded components, the shared-course names, and the
dataclass are built for survivors. `_rank` uses `argpartition` for the cut —
which splits on the total alone and breaks ties arbitrarily, so it widens the
candidate set to everything at or above the boundary before sorting. That is
what makes it identical to slicing a full sort, ties included. If you change the
tie-break, change it there. `TestTopNSelection` pins the boundary case.

Two things measured and *not* done, so they don't get re-proposed as free wins:
memoizing `soft_jaccard` across alumni (the real corpus has 487 distinct
major-match keys per 611 alumni — ~10% for a real bit-identical-float risk), and
vectorizing it (fuzzy text; can't be kept bit-identical by inspection).

`build_corpus` must stay a *pure restatement* of the accessors it replaces — the
moment a field is computed differently from its inline source it stops being an
optimization and becomes an unreviewed change to the ranking.
`tests/test_corpus.py` pins each field to its source. Note the set fields are
deliberately not frozensets: `soft_jaccard` sums a `max()` per element, so
iteration order fixes float summation order, and `frozenset(s)` need not iterate
like `s`.

**`Corpus` carries its `school_id` and the job asserts it against the student's.**
Batching is what makes a cross-tenant mix-up possible at all, so the pairing is
checked rather than trusted. `load_corpus` refuses `school_id=None` for the same
reason `current_student` refuses a null tenant.

**The response contains no geometry.** Radius, angle, and coordinates belong to
the frontend; shipping them would freeze its layout model. A test asserts this.

**Programs are set-valued, never scalar.** A person's majors/minors live in a
role-tagged join table (`alumnus_majors` / `student_program`), read *only*
through [app/matching/programs.py](app/matching/programs.py). A test asserts no
other module touches the program field directly. `Student.declared_major` is a
deprecated mirror kept for one release — `student_majors()` falls back to it,
which means a test can pass on the fallback while the join-table write is
broken. Assert on the table when you touch this.

**Ingestion is source-swappable.** Adapters yield the source-neutral dataclasses
in `app/ingest/records.py`; `loader.py` is the only ORM-facing code. Nothing
downstream learns which dataset ran. The tenant travels as
`AlumnusRecord.school_name` — deliberately *not* read off `outcome_org`, which
means "employer" the moment a source carries real career data.

**Match rationales are derived, not written.** `app/matching/explain.py` builds
`matchReason` from the same components that produced the ranking, and never
mentions the alumnus's career outcome — that outcome is synthetic on the
placeholder dataset, and a rationale is where invented data would read as fact.
If you are ever tempted to have a model write these, don't: an explanation that
disagrees with the ranking is worse than none.

**Provenance labeling.** `synthetic` (seeded employment) and `derived` (inferred
minors) data must surface its provenance anywhere it reaches the UI. MIDFIELD
has no employment data at all; the career outcomes are a stand-in. Never present
either as reported fact.

**Explore facets filter, they never score.** `interests`, `careerArea`, and
`major` narrow the corpus in [app/matching/facets.py](app/matching/facets.py);
the weighted formula then ranks whoever survives. Keep it that way — folding a
facet into the score would let a strong course overlap outvote an explicit "show
me Health Policy", and it would turn every new facet into a scoring change
needing an eval run.

The career-area facet matches `outcome_labels` (academic area + industry), *not*
`destinations`, which includes final majors. That distinction is load-bearing:
matching majors made `careerArea=Aerospace & Defense` return 65 alumni of whom
only 50 worked in it, because Aerospace Engineering graduates share a token with
the industry name. `TestCareerArea` pins it.

**Every facet is in the cache key.** `ExploreQuery` ([app/jobs/recompute.py](app/jobs/recompute.py))
owns the hash, the index params, and the rebuild — one object because those three
have to stay in step. Interests are sorted, so chip order can't fork the cache
into two entries holding the same payload. Adding a query parameter that changes
the payload without adding it here writes a filtered result over the unfiltered
entry.

## Changing the scoring formula

Don't do it by inspection — run `python -m app.eval` before and after, and put
the before/after `lift` numbers in the PR. The engine is only ~1.4x better than
chance at predicting real destinations today, so a change that *feels* right is
well within the noise you can't see by reading a diff.

Two failure modes the harness exists to catch, both of which have already
happened here:

- **A component that ranks nothing.** `major_match` sat at a constant 0.25 for
  every alumnus in the corpus, consuming 20% of the weight, while every unit
  test passed — a per-alumnus assertion cannot see a corpus-wide constant.
  `TestComponentsDiscriminate` in `tests/test_scoring.py` is the cheap canary;
  the harness is the real check.
- **A component that varies but doesn't reorder.** Ablation catches these; a
  rank correlation of 1.0 means the component is inert regardless of spread.

When adding an eval, never let the held-out profile carry the label. The
destination lives in `intended_direction`, which `major_match` scores directly —
filling it in makes the metrics look excellent and measure nothing.

## Conventions

- **camelCase on the wire, snake_case in Python.** All response schemas extend
  `CamelModel`; routes pass `response_model_by_alias=True`.
- **Repository, not raw queries in routes.** Relationships are eager-loaded
  (`selectinload`) because the scorer touches every alumnus's courses, majors,
  and pivots — lazy loading means thousands of round trips per request.
- **`get_student` uses `populate_existing`.** Sessions run with
  `expire_on_commit=False`, so without it a read-after-write returns the
  collections the instance had *before* the write. This bit twice already.
- **`semester_index` (0–7) is the source of truth for timing.** Labels like
  "Sophomore Spring" are derived. Keeps pre-pivot filtering and year alignment
  as integer comparisons.
- **Comments explain *why*.** This codebase documents decisions and rejected
  alternatives, not mechanics. Match that register — don't add comments that
  restate the line below them.
- **Alumni have no name column**, by construction. Don't add one.

## Testing

`asyncio_mode = "auto"` with a **session-scoped event loop** — asyncpg binds
pooled connections to the loop that opened them, so a per-test loop leaves the
shared engine holding connections to a closed one. If you see
`RuntimeError: Event loop is closed`, that's the cause.

The matching engine takes plain ORM objects and never touches a session, so
everything except `test_api_security.py` runs without Postgres. Keep it that
way: build test fixtures with `tests/factories.py` rather than reaching for the
database.

DB-backed tests namespace their rows (`test-sec-*`) and clean up after
themselves, because the suite may run against a development database that has
real ingested data in it. Don't write a test that truncates a table.

## Gotchas

- **Don't run `scripts/seed.py --reset` casually.** It deletes every student and
  alumnus. A dev database may hold a MIDFIELD corpus that took a long ingest to
  build. Schools survive a reset on purpose — students created through the API
  reference them, and dropping a school cascades those accounts away.
- **`.env` is the developer's local file.** Edit `.env.example` instead.
- Migrations form a linear chain; the current head is `e9a4c1f27b30`.
- Compose maps Postgres to **5433** and Redis to **6380** to avoid colliding
  with local instances. The defaults in `config.py` match.
