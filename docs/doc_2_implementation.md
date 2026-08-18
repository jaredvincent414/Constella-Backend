# Constella — System Design & Data Storage Plan

**Date:** August 17, 2026
**Status:** Draft — ready for backend implementation

---

## Design decision: PostgreSQL, not a graph database

Every frontend feature was traced against a relational schema. All queries are fixed-depth joins (2-4 tables), the dataset is small per school (~5k alumni, ~500 courses, ~50k enrollment records), and no query requires variable-depth traversal. Postgres handles every endpoint in single-digit milliseconds with basic indexing.

The graph mental model informed the schema design — entities and named relationships with temporal properties — but the storage engine is Postgres.

---

## 1. Database schema

### Core entities

```sql
-- Schools (multi-tenant: each deployment is scoped to a school)
CREATE TABLE schools (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    slug        TEXT UNIQUE NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Students (active users)
CREATE TABLE students (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    email           TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    first_name      TEXT NOT NULL,
    last_name       TEXT NOT NULL,
    current_year    TEXT NOT NULL,          -- 'Freshman', 'Sophomore', etc.
    declared_major  TEXT,                   -- nullable for undeclared
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Courses (canonical course catalog per school)
CREATE TABLE courses (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id   UUID NOT NULL REFERENCES schools(id),
    code        TEXT NOT NULL,              -- 'BIO 101'
    name        TEXT NOT NULL,              -- 'Introduction to Biology'
    department  TEXT NOT NULL,
    credits     SMALLINT DEFAULT 3,
    UNIQUE (school_id, code)
);

-- Academic programs (majors, minors, concentrations)
CREATE TABLE programs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id   UUID NOT NULL REFERENCES schools(id),
    name        TEXT NOT NULL,              -- 'Biochemistry'
    type        TEXT NOT NULL,              -- 'major', 'minor', 'concentration'
    department  TEXT,
    UNIQUE (school_id, name, type)
);

-- Course-to-program mapping (which courses fulfill which programs)
CREATE TABLE program_requirements (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    program_id  UUID NOT NULL REFERENCES programs(id),
    course_id   UUID NOT NULL REFERENCES courses(id),
    requirement_type TEXT DEFAULT 'elective', -- 'core', 'elective', 'prerequisite'
    UNIQUE (program_id, course_id)
);
```

### Alumni (the core data asset)

```sql
-- Alumni records (anonymized, no PII — identified by school-scoped ID)
CREATE TABLE alumni (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    school_id       UUID NOT NULL REFERENCES schools(id),
    graduation_year SMALLINT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Alumni-to-program declarations (the DECLARED edge, with temporal data)
-- An alumnus who switched majors has multiple rows with different semesters
CREATE TABLE alumni_program_declarations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alumni_id       UUID NOT NULL REFERENCES alumni(id),
    program_id      UUID NOT NULL REFERENCES programs(id),
    declared_semester TEXT NOT NULL,         -- 'Freshman Fall'
    dropped_semester  TEXT,                  -- null if kept through graduation
    is_final        BOOLEAN DEFAULT false    -- true = on diploma at graduation
);
CREATE INDEX idx_apd_alumni ON alumni_program_declarations(alumni_id);
CREATE INDEX idx_apd_program ON alumni_program_declarations(program_id);

-- Alumni course enrollments (the TOOK edge, with temporal data)
-- This is the highest-volume table and the primary matching dimension
CREATE TABLE alumni_enrollments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alumni_id   UUID NOT NULL REFERENCES alumni(id),
    course_id   UUID NOT NULL REFERENCES courses(id),
    semester    TEXT NOT NULL,              -- 'Sophomore Fall'
    UNIQUE (alumni_id, course_id, semester)
);
CREATE INDEX idx_ae_alumni ON alumni_enrollments(alumni_id);
CREATE INDEX idx_ae_course ON alumni_enrollments(course_id);
CREATE INDEX idx_ae_course_alumni ON alumni_enrollments(course_id, alumni_id);

-- Career outcomes (the terminal node — where alumni ended up)
CREATE TABLE career_outcomes (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alumni_id   UUID NOT NULL REFERENCES alumni(id) UNIQUE,
    title       TEXT NOT NULL,              -- 'Health Policy Analyst'
    company     TEXT,                       -- 'State Health Department'
    industry    TEXT NOT NULL,              -- 'Health Policy'
    years_after_graduation SMALLINT DEFAULT 1
);

-- Alumni interests / extracurriculars
CREATE TABLE alumni_interests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alumni_id   UUID NOT NULL REFERENCES alumni(id),
    interest    TEXT NOT NULL,
    UNIQUE (alumni_id, interest)
);
CREATE INDEX idx_ai_interest ON alumni_interests(interest);
```

### Student activity tables

```sql
-- Student course enrollments (what the student has taken so far)
CREATE TABLE student_enrollments (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id  UUID NOT NULL REFERENCES students(id),
    course_id   UUID NOT NULL REFERENCES courses(id),
    semester    TEXT NOT NULL,
    UNIQUE (student_id, course_id, semester)
);

-- Student interests
CREATE TABLE student_interests (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id  UUID NOT NULL REFERENCES students(id),
    interest    TEXT NOT NULL,
    UNIQUE (student_id, interest)
);

-- Saved paths (bookmarked alumni trajectories)
CREATE TABLE saved_paths (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id  UUID NOT NULL REFERENCES students(id),
    alumni_id   UUID NOT NULL REFERENCES alumni(id),
    color_index SMALLINT DEFAULT 0,
    saved_at    TIMESTAMPTZ DEFAULT now(),
    UNIQUE (student_id, alumni_id)
);

-- Activity log (recent actions for dashboard)
CREATE TABLE activity_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id  UUID NOT NULL REFERENCES students(id),
    action_type TEXT NOT NULL,              -- 'explore', 'simulate', 'save_path'
    description TEXT NOT NULL,              -- 'Explored Health Policy cluster'
    metadata    JSONB,                      -- flexible payload for action-specific data
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_activity_student ON activity_log(student_id, created_at DESC);
```

### Search support

```sql
-- Full-text search across courses, programs, industries
-- Uses Postgres built-in tsvector — no external search engine needed at this scale
ALTER TABLE courses ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', name || ' ' || code || ' ' || department)) STORED;
CREATE INDEX idx_courses_search ON courses USING GIN(search_vector);

ALTER TABLE programs ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', name || ' ' || coalesce(department, ''))) STORED;
CREATE INDEX idx_programs_search ON programs USING GIN(search_vector);

ALTER TABLE career_outcomes ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('english', title || ' ' || industry || ' ' || coalesce(company, ''))) STORED;
CREATE INDEX idx_outcomes_search ON career_outcomes USING GIN(search_vector);
```

---

## 2. Endpoint-to-query mapping

Every frontend endpoint mapped to the exact SQL pattern that serves it. No query exceeds 4 joins.

### `POST /auth/login` and `POST /auth/signup`

Standard auth — single table lookup/insert on `students`. Hash passwords with bcrypt. Return a signed JWT containing `student_id` and `school_id`.

```
Login:  SELECT id, password_hash FROM students WHERE email = $1
Signup: INSERT INTO students (...) VALUES (...) RETURNING id
```

Joins: 0. Complexity: trivial.

---

### `GET /me` → StudentProfile

```sql
-- 1. Student record
SELECT id, first_name, last_name, email, current_year, declared_major
FROM students WHERE id = $student_id;

-- 2. Their school
SELECT name FROM schools WHERE id = $school_id;

-- 3. Courses completed (1 join)
SELECT c.id, c.name, se.semester
FROM student_enrollments se
JOIN courses c ON c.id = se.course_id
WHERE se.student_id = $student_id
ORDER BY se.semester;

-- 4. Interests
SELECT interest FROM student_interests WHERE student_id = $student_id;
```

Joins: 1 (enrollment → course). All indexed. Assembles into the StudentProfile JSON shape in application code.

---

### `GET /explore` → ConstellationData

This is the most complex query — the cohort matching engine. Still clean.

```sql
-- Step 1: Get the student's course IDs
SELECT course_id FROM student_enrollments WHERE student_id = $student_id;

-- Step 2: Find alumni who took overlapping courses, scoped to school (2 joins)
SELECT
    ae.alumni_id,
    COUNT(ae.course_id) AS course_overlap
FROM alumni_enrollments ae
JOIN alumni a ON a.id = ae.alumni_id
WHERE ae.course_id = ANY($student_course_ids)
  AND a.school_id = $school_id
GROUP BY ae.alumni_id
HAVING COUNT(ae.course_id) >= 1;

-- Step 3: For matched alumni, fetch full records (batch queries)
-- 3a. Programs
SELECT apd.alumni_id, p.name, p.type, apd.is_final
FROM alumni_program_declarations apd
JOIN programs p ON p.id = apd.program_id
WHERE apd.alumni_id = ANY($matched_alumni_ids) AND apd.is_final = true;

-- 3b. Career outcomes
SELECT alumni_id, title, industry, company
FROM career_outcomes WHERE alumni_id = ANY($matched_alumni_ids);

-- 3c. All courses by semester (for detail panel)
SELECT ae.alumni_id, ae.semester, c.id, c.name
FROM alumni_enrollments ae
JOIN courses c ON c.id = ae.course_id
WHERE ae.alumni_id = ANY($matched_alumni_ids)
ORDER BY ae.alumni_id, ae.semester;

-- 3d. Pivot points (derived from major declarations)
SELECT alumni_id, declared_semester, dropped_semester, p.name AS program_name
FROM alumni_program_declarations apd
JOIN programs p ON p.id = apd.program_id
WHERE apd.alumni_id = ANY($matched_alumni_ids)
  AND apd.dropped_semester IS NOT NULL;

-- 3e. Interests
SELECT alumni_id, interest
FROM alumni_interests WHERE alumni_id = ANY($matched_alumni_ids);
```

Joins per query: 1-2. The pattern is "find matches → batch-fetch details." All use indexed columns. Optional filters (`career_area`, `major`, `interests`) add WHERE clauses to Step 2.

**Similarity scoring** happens in application code, not SQL. The scoring function combines:

- Course overlap count (heaviest weight, from Step 2)
- Year alignment (compare student's `current_year` to alumnus's pivot/graduation timing)
- Interest overlap (set intersection of student interests vs alumni interests)
- Recency bonus (more recent `graduation_year` scores slightly higher)

The score is computed per alumnus, results sorted, and the response assembled.

**Clustering** is computed in application code by grouping alumni by `career_outcome.industry`. Cluster `x`/`y` positions can be derived deterministically (e.g. distribute clusters evenly on a circle) or precomputed. The `topMajors` per cluster is a simple frequency count over the grouped alumni's programs.

**Course status field** (`kept`, `new`, `dropped`) is derived by comparing the student's course list to the alumnus's course list. For each alumnus:
- Courses the student has also taken → `"kept"`
- Courses the student hasn't taken → `"new"`
- Courses the student took but the alumnus didn't → `"dropped"` (only relevant in What-If context)

This is a set comparison in application code, not a database concern.

---

### `POST /simulate` → TransitionSimulation

```sql
-- Step 1: Find alumni who pivoted from major A to major B (2 joins)
SELECT DISTINCT apd_from.alumni_id
FROM alumni_program_declarations apd_from
JOIN alumni_program_declarations apd_to ON apd_to.alumni_id = apd_from.alumni_id
JOIN alumni a ON a.id = apd_from.alumni_id
JOIN programs p_from ON p_from.id = apd_from.program_id
JOIN programs p_to ON p_to.id = apd_to.program_id
WHERE p_from.name = $from_major
  AND apd_from.dropped_semester IS NOT NULL
  AND p_to.name = $to_major
  AND apd_to.is_final = true
  AND a.school_id = $school_id;

-- Step 2: Same batch-fetch pattern as /explore for matched alumni
-- (courses by semester, outcomes, programs, interests)

-- Step 3: Derive summary stats in application code
-- - totalTransitions = count of matched alumni
-- - peakTiming = mode of pivot semesters
-- - topOutcome = most common career_outcome.industry
-- - cards = ranked by similarity score, top 5
```

Joins: 4 in Step 1 (the most complex query in the system). This is a filtered join — "find alumni where program_declaration X was dropped and program_declaration Y was final." Postgres handles this trivially. The `timeline` for each card is built in application code from the semester-ordered course data.

---

### `GET /dashboard/stats`

```sql
-- All four stats in one round-trip using subqueries
SELECT
    (SELECT COUNT(DISTINCT alumni_id) FROM saved_paths WHERE student_id = $sid) AS saved_count,
    (SELECT COUNT(*) FROM activity_log WHERE student_id = $sid AND action_type = 'explore') AS clusters_explored;

-- Highest match + alumni match count come from running the /explore similarity
-- scoring against cached or freshly computed results. Can be cached in a
-- materialized view or computed on-demand (fast enough at this scale).
```

Joins: 0. Just counts.

---

### `GET /saved-paths`, `POST /saved-paths`, `DELETE /saved-paths/:id`

```sql
-- List saved paths (2 joins to hydrate alumni data)
SELECT sp.id, sp.color_index, sp.saved_at,
       a.graduation_year,
       co.title, co.industry, co.company
FROM saved_paths sp
JOIN alumni a ON a.id = sp.alumni_id
JOIN career_outcomes co ON co.alumni_id = a.id
WHERE sp.student_id = $student_id
ORDER BY sp.saved_at DESC;

-- For each saved path, batch-fetch programs + courses (same pattern as /explore)

-- Save a path
INSERT INTO saved_paths (student_id, alumni_id, color_index) VALUES ($1, $2, $3);

-- Delete a path
DELETE FROM saved_paths WHERE id = $1 AND student_id = $student_id;
```

Joins: 2 for listing. CRUD is single-table.

---

### `GET /search?q=...`

```sql
-- Full-text search across three tables, union results
SELECT 'course' AS type, id, name AS label, department AS context
FROM courses WHERE search_vector @@ plainto_tsquery('english', $query) AND school_id = $school_id
UNION ALL
SELECT 'program' AS type, id, name AS label, type AS context
FROM programs WHERE search_vector @@ plainto_tsquery('english', $query) AND school_id = $school_id
UNION ALL
SELECT 'outcome' AS type, alumni_id AS id, title AS label, industry AS context
FROM career_outcomes WHERE search_vector @@ plainto_tsquery('english', $query)
LIMIT 20;
```

Joins: 0. Uses GIN indexes on tsvector columns. Fast at any reasonable scale.

---

### `GET /activity?limit=4`

```sql
SELECT action_type, description, created_at
FROM activity_log
WHERE student_id = $student_id
ORDER BY created_at DESC
LIMIT $limit;
```

Joins: 0. Single indexed scan.

---

## 3. System architecture

```
┌─────────────────────────────────────────────────────┐
│  Frontend (React + D3)                              │
│  app/src/lib/api.ts → http://localhost:8000         │
└──────────────────────┬──────────────────────────────┘
                       │ JSON over HTTPS
                       ▼
┌─────────────────────────────────────────────────────┐
│  API Server (Python / FastAPI)                      │
│                                                     │
│  /auth/*        → Auth service (bcrypt + JWT)       │
│  /me            → Student service                   │
│  /explore       → Matching engine                   │
│  /simulate      → Simulation engine                 │
│  /saved-paths   → CRUD service                      │
│  /search        → Search service                    │
│  /dashboard/*   → Dashboard service                 │
│                                                     │
│  Scoring layer:                                     │
│  - Course overlap (SQL count)                       │
│  - Year alignment (application code)                │
│  - Interest overlap (set intersection)              │
│  - Recency weighting (application code)             │
│  - Score normalization to 0-100                     │
│                                                     │
│  Clustering layer:                                  │
│  - Group by career_outcome.industry                 │
│  - Compute topMajors per cluster                    │
│  - Assign x/y positions (deterministic layout)      │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│  PostgreSQL                                         │
│                                                     │
│  12 tables, 7 indexes                               │
│  Full-text search via tsvector + GIN                │
│  No extensions required for v1                      │
│  (pgvector optional for future embedding search)    │
└─────────────────────────────────────────────────────┘
```

### Key design decisions

**Multi-tenancy via school_id.** Every query is scoped to a school. Alumni at School A are never mixed with School B. This is enforced at the query level (WHERE school_id = $school_id) using the school_id from the student's JWT.

**Scoring in application code, not SQL.** The similarity score combines multiple dimensions with tunable weights. Putting this in SQL would make it rigid and hard to iterate on. The SQL finds candidates (course overlap ≥ 1), then application code scores and ranks them.

**Clustering in application code.** The initial version groups by `career_outcome.industry`. This can evolve to k-means or embedding-based clustering later without changing the database schema.

**Course status is derived, not stored.** The `status` field (`kept`, `new`, `dropped`) on each course is computed per-request by comparing the student's courses to the alumnus's courses. This keeps the database normalized and avoids stale derived data.

**Pivot points are derived from major declarations.** An alumnus who has a `dropped_semester` on one program declaration and an `is_final = true` on another has a pivot point. The `fromMajor`, `toMajor`, and `semester` are all derivable from the `alumni_program_declarations` table without storing explicit pivot records.

---

## 4. API response assembly patterns

The backend assembles frontend JSON shapes from flat SQL results. Two patterns cover every endpoint:

### Pattern A: Single-entity hydration (used by /me)

Query the entity, then query its related tables. Assemble in code.

```python
student = db.fetch_one("SELECT ... FROM students WHERE id = $1", student_id)
courses = db.fetch_all("SELECT ... FROM student_enrollments JOIN courses ...")
interests = db.fetch_all("SELECT ... FROM student_interests ...")
return StudentProfile(
    id=student.id,
    coursesCompleted=courses,
    interests=[i.interest for i in interests],
    ...
)
```

### Pattern B: Batch-entity hydration (used by /explore, /simulate, /saved-paths)

Find matching entity IDs, then batch-fetch all related data for those IDs. Group in code.

```python
# Step 1: Find alumni IDs + overlap counts
matches = db.fetch_all("SELECT alumni_id, COUNT ... GROUP BY alumni_id", ...)

alumni_ids = [m.alumni_id for m in matches]

# Step 2: Batch-fetch (3-5 parallel queries, all use ANY($alumni_ids))
programs = db.fetch_all("SELECT ... WHERE alumni_id = ANY($1)", alumni_ids)
outcomes = db.fetch_all("SELECT ... WHERE alumni_id = ANY($1)", alumni_ids)
enrollments = db.fetch_all("SELECT ... WHERE alumni_id = ANY($1)", alumni_ids)
interests = db.fetch_all("SELECT ... WHERE alumni_id = ANY($1)", alumni_ids)

# Step 3: Group by alumni_id using dict comprehension
programs_by_alumni = group_by(programs, key=lambda p: p.alumni_id)
# ... same for others

# Step 4: Assemble, score, rank, cluster
alumni_records = [
    build_alumni_record(aid, programs_by_alumni[aid], outcomes[aid], ...)
    for aid in alumni_ids
]
scored = [(a, compute_similarity(student, a)) for a in alumni_records]
scored.sort(key=lambda x: x[1], reverse=True)
clusters = group_into_clusters(scored)
```

This pattern avoids N+1 queries entirely. For 50 matched alumni, it's 5-6 total SQL queries regardless of result count.

---

## 5. Data pipeline: ingesting alumni records

Alumni data comes from school registrar offices and career services. The ingestion pipeline:

```
Registrar data (CSV/API)     Career services data
        │                            │
        ▼                            ▼
┌─────────────────────────────────────────┐
│  ETL pipeline (Python scripts)          │
│                                         │
│  1. Parse transcript data               │
│  2. Normalize course names/codes        │
│  3. Extract major declaration timeline  │
│  4. Match career outcomes to alumni     │
│  5. Anonymize (strip PII)              │
│  6. Insert into Postgres                │
└─────────────────────────────────────────┘
```

Each school onboarding produces a one-time bulk import. Updates are periodic (yearly, when new graduating classes are added).

Data volumes per school (estimated):
- Alumni records: 2,000-10,000
- Courses in catalog: 200-2,000
- Enrollment records: 20,000-100,000
- Career outcomes: 2,000-10,000
- Program declarations: 4,000-20,000

Total per school: well under 1M rows across all tables. Postgres handles this without any performance tuning.

---

## 6. Frontend type mismatch resolution

The handoff doc notes a mismatch between `SimulationResult` (what api.ts types) and `TransitionSimulation` (what the Transition page renders). Resolution:

**Backend returns `TransitionSimulation` directly.** The `cards` array with per-alumni timelines is the right shape. The backend computes `peakTiming`, `topOutcome`, `topOutcomeCount`, and `totalTransitions` as summary stats from the query results.

The frontend should update `api.ts` to type the `/simulate` response as `TransitionSimulation` and remove the `SimulationResult` type.

---

## 7. Implementation order

Phase 1 — core loop (unblocks frontend integration):
1. Database schema migration (all tables)
2. `POST /auth/signup` + `POST /auth/login` (JWT)
3. `GET /me` (student profile)
4. `GET /explore` (matching engine + clustering)
5. `POST /simulate` (transition engine)
6. Seed database with test data (1 school, 200 alumni)

Phase 2 — complete the feature set:
7. `GET/POST/DELETE /saved-paths`
8. `GET /dashboard/stats`
9. `GET /activity` (activity logging)
10. `GET /search` (full-text)

Phase 3 — scoring improvements:
11. Tune similarity weights based on user feedback
12. Evaluate pgvector for embedding-based matching
13. Evaluate smarter clustering (k-means on career outcomes)

---

## 8. What this does NOT need

| Technology | Why not |
|---|---|
| Graph database (Neo4j, etc.) | All queries are fixed-depth (2-4 joins), dataset is small. Postgres handles every endpoint. |
| Redis / caching layer | Under 1M rows per school. Queries return in milliseconds. Add caching only if latency becomes measurable. |
| Elasticsearch | Postgres tsvector + GIN handles full-text search at this scale. Revisit if search gets complex (fuzzy, faceted, autocomplete). |
| Message queue | No async workflows in v1. Activity logging is synchronous and fast. |
| Microservices | Single FastAPI app is the right call for a team this size. Split later if needed. |
