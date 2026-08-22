# Admin Portal — Backend Implementation Plan

Created: 2026-08-21

## Context

Schools need a way to connect their alumni data to Constella via NSC (National Student Clearinghouse) and PESC (Postsecondary Electronic Standards Council) infrastructure. Constella's core promise is **transparency** — schools must see exactly what happens to their data, what's kept, what's discarded, and have a full audit trail.

The backend already has a clean integration seam: the `SourceAdapter` protocol in `app/ingest/base.py`. New adapters produce the same `AlumnusRecord`/`CourseRecord`/`MajorRecord` dataclasses, and the loader, scorer, clustering, and API work unchanged.

**What Constella already stores (no PII by design)**:
- `Alumnus`: anonymous id, school_id, graduation_year, career_area — no name column
- `AlumnusCourse`: course_code, course_name, semester_index, dropped, discipline, credit_hours
- `AlumnusMajor`: name, cip6, declared_semester, is_final, role, provenance
- `Pivot`, `Milestone`, `CareerOutcome` — academic events only

---

## Sensitive Data Architecture

### The Privacy Pipeline

PESC College Transcript XML contains full student records — names, DOBs, SSNs, addresses. Constella must process this data without persisting any of it. The architecture enforces this structurally, not by policy.

```
┌─────────────────────────────────────────────────────────────────┐
│                     INGESTION BOUNDARY                          │
│  PESC XML arrives via POST /api/ingest/pesc/{school}            │
│  or NSC pull via ETX/NextGen                                    │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                  PII EXTRACTION ZONE (in-memory only)           │
│                                                                 │
│  1. Parse XML with streaming iterparse (bounded memory)         │
│  2. For each <Student> element:                                 │
│     a. Read Person/Name + Person/Birth → compute anonymous hash │
│     b. Read AcademicRecord → extract courses, majors, pivots    │
│     c. DISCARD all Person/* fields from memory                  │
│     d. Yield AnonymizedResult (hash + academic data only)       │
│                                                                 │
│  Nothing in this zone touches disk or database.                 │
│  No PII field is ever assigned to an ORM model attribute.       │
│  No PII field is ever written to a log message.                 │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                  AUDIT GENERATION                                │
│                                                                 │
│  Before discarding, generate an IngestAuditEntry per record:    │
│  - anonymous_id (the hash)                                      │
│  - fields_received: ["name", "dob", "ssn", "address", ...]     │
│  - fields_retained: ["courses", "majors", "graduation_year"]   │
│  - fields_discarded: ["name", "dob", "ssn", "address", ...]    │
│  - validation_flags: ["missing_cip", "no_courses", ...]        │
│  - record_status: accepted / skipped / error                   │
│                                                                 │
│  This is the transparency layer — schools see exactly what      │
│  happened to each record without seeing the PII itself.         │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                  PERSISTENCE BOUNDARY                            │
│                                                                 │
│  Only AlumnusRecord dataclasses cross this line.                │
│  The loader writes to alumni, alumnus_courses, alumnus_majors,  │
│  pivots, milestones, career_outcomes — all PII-free by schema.  │
│                                                                 │
│  IngestAuditEntry records are persisted for the admin dashboard.│
│  SyncJob is updated with aggregate stats.                       │
└─────────────────────────────────────────────────────────────────┘
```

### Anonymous ID Generation

```python
# HMAC-SHA256 with a server-side secret — resistant to re-identification
# even by the data source, which knows its own roster.
import hmac, hashlib

def anonymous_id(school_id: str, first_name: str, last_name: str, birth_date: str) -> str:
    """Deterministic, stable across re-syncs, PII-free after computation."""
    msg = f"{school_id}:{last_name.lower().strip()}:{first_name.lower().strip()}:{birth_date}"
    mac = hmac.new(
        key=settings.alumni_hmac_secret.encode(),   # from SchoolConfig or app config
        msg=msg.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"pesc-{mac[:24]}"
```

- **HMAC, not bare SHA-256.** A plain hash over guessable inputs (name + DOB) is trivially reversible by anyone with the school roster. The server-side secret makes the mapping one-way even for the data source.
- Same person → same HMAC → upsert on re-sync (no duplicates)
- 24-char hex (96 bits) keeps collision probability negligible across large corpora
- Inputs are case-normalized and discarded immediately after HMAC computation
- `alumni_hmac_secret` is a required config value — app refuses to start without it
- If a source lacks DOB, fall back to `school_id:last_name:first_name:grad_year` (weaker — logged as a validation flag)

### What Never Touches Disk

| PESC Field | Used For | Persisted? |
|-----------|----------|------------|
| `Person/Name/FirstName`, `LastName` | Anonymous hash input | No |
| `Person/Birth/BirthDate` | Anonymous hash input | No |
| `Person/SSN` | Nothing — ignored entirely | No |
| `Person/Contacts/Address` | Nothing — ignored entirely | No |
| `Person/Contacts/Phone` | Nothing — ignored entirely | No |
| `Person/Contacts/Email` | Nothing — ignored entirely | No |

### What Gets Persisted

| PESC Field | Constella Field | Why |
|-----------|----------------|-----|
| Course codes + names | `AlumnusCourse` | Core of matching engine |
| CIP codes | `AlumnusMajor.cip6` | Major classification |
| Program names + types | `AlumnusMajor` | Pivot detection, matching |
| Session dates (Fall/Spring) | `semester_index` (0-7) | Timeline construction |
| Award dates | `graduation_year` | Cohort filtering |
| Dropped status (W/WF grades) | `AlumnusCourse.dropped` | Academic path accuracy |
| Credit hours | `AlumnusCourse.credit_hours` | Course weight |

---

## Phase 1: PESC Push + Admin Infrastructure

### 1. New ORM Models — `app/models/admin.py`

**SchoolConfig** (1:1 with School):
- `school_id` FK to schools.id
- `integration_mode`: enum (pesc_push / nsc_pull / manual)
- `pesc_endpoint_token`: SHA-256 of the per-school bearer token
- `admin_email`
- `created_at`, `updated_at`

**AdminUser** (separate from Student):
- `id` (int PK)
- `school_id` FK
- `email` (unique)
- `password_hash` (scrypt, same scheme as `app/auth.py`)
- `name`
- `role`: enum (owner / viewer) — owner can rotate tokens, viewer can only inspect
- `created_at`

**SyncJob** (audit trail for every sync):
- `id` (int PK)
- `school_id` FK
- `source_type`: enum (pesc_push / nsc_pull)
- `status`: enum (pending / running / completed / failed)
- `records_received` (int) — total records in the payload
- `records_accepted` (int) — records that passed validation and were loaded
- `records_skipped` (int) — records that failed validation
- `records_updated` (int) — existing records that were upserted
- `error_detail` (text, nullable)
- `pii_fields_encountered` (ARRAY string) — which PII fields were present and discarded (e.g. ["name", "dob", "ssn"])
- `started_at`, `completed_at`, `created_at`
- Index on `(school_id, created_at DESC)`

**IngestAuditEntry** (per-record audit — the transparency layer):
- `id` (int PK)
- `sync_job_id` FK
- `anonymous_id` — the hashed alumni ID
- `record_status`: enum (accepted / skipped / updated / error)
- `fields_received` (ARRAY string) — all field categories present in the source record
- `fields_retained` (ARRAY string) — what was kept
- `fields_discarded` (ARRAY string) — what was dropped (PII categories, not values)
- `validation_flags` (ARRAY string) — issues found (e.g. "missing_cip", "no_courses", "no_award_date")
- `courses_count` (int)
- `majors_count` (int)
- `has_pivots` (bool)
- `graduation_year` (int, nullable)
- `error_message` (text, nullable)
- `created_at`
- Index on `(sync_job_id, record_status)`

Alembic migration for all four tables.

### 2. PESC XML Adapter — `app/ingest/sources/pesc.py`

Implements `SourceAdapter` protocol. Constructor takes XML content (bytes) and school slug.

**PESC College Transcript XML mapping**:

```
CollegeTranscript
  └─ Student
       ├─ Person
       │    ├─ Name/FirstName, LastName  ──→ hash input, then DISCARD
       │    ├─ Birth/BirthDate           ──→ hash input, then DISCARD
       │    └─ SSN, Contacts             ──→ IGNORE entirely
       └─ AcademicRecord
            ├─ AcademicSession[]
            │    ├─ SessionDesignator     ──→ semester_index mapping
            │    ├─ Course[]
            │    │    ├─ SubjectAbbrev + Number ──→ CourseRecord.code
            │    │    ├─ CourseTitleLong        ──→ CourseRecord.name
            │    │    ├─ GradeStatusCode (W/WF) ──→ CourseRecord.dropped
            │    │    ├─ CreditEarned           ──→ CourseRecord.credit_hours
            │    │    └─ CIPCode                ──→ CourseRecord.discipline
            │    └─ StudentAcademicProgram[]
            │         ├─ ProgramName            ──→ MajorRecord.name
            │         ├─ ProgramType            ──→ MajorRecord.role
            │         └─ CIPCode                ──→ MajorRecord.cip6
            └─ AcademicAward[]
                 ├─ AwardDate                   ──→ graduation_year
                 └─ AwardProgram/ProgramName    ──→ final major (is_final=True)
```

Extract parsing functions as importable utilities (not just class methods) so the NSC adapter reuses them in Phase 2.

Pivot detection: walk programs chronologically; when primary major CIP changes between sessions, emit `PivotRecord`.

Career outcomes: PESC has no employment data. Use degree CIP family as `career_area` with `provenance='synthetic'`. Real career data comes Phase 3.

Also yields `IngestAuditEntry` data alongside each `AlumnusRecord` for the transparency layer.

### 3. Incremental Loader — `app/ingest/loader.py`

Add `load_incremental(adapter, session, school_id) -> LoadStats`:
- For each `AlumnusRecord`, check if `alumni.id` exists
- If exists: delete (cascades children), re-insert, count as `records_updated`
- If new: insert, count as `records_accepted`
- Batch commits (same pattern as existing `load()`)
- Return stats for `SyncJob` population

### 4. Admin Auth — `app/auth.py`

- Reuse existing `hash_password()`/`verify_password()` for AdminUser
- Add JWT encode/decode (`PyJWT` — single new dependency)
- Add `current_admin_user` FastAPI dependency: validates JWT from Authorization header, returns AdminUser
- Initial admin creation via CLI: `python -m app.admin create-admin --email X --school Y`

### 5. PESC Receive Endpoint — `app/api/routes/admin_portal.py`

**Machine-to-machine** (per-school token auth):

`POST /api/ingest/pesc/{school_slug}`
- Validates per-school token from `SchoolConfig.pesc_endpoint_token` (constant-time compare)
- Creates `SyncJob` (status=pending)
- Launches background task: parse with `PescAdapter`, load with `load_incremental()`, generate `IngestAuditEntry` rows, update `SyncJob`, trigger `recompute` for affected school
- Returns `202 Accepted` with `{sync_id, status}`

### 6. Admin API Endpoints — `app/api/routes/admin_portal.py`

All require `current_admin_user` JWT auth:

**School config**:
- `GET /api/admin-portal/school` — integration mode, endpoint URL, last sync time
- `POST /api/admin-portal/school/rotate-token` — generate new PESC token (owner role only)

**Sync management**:
- `GET /api/admin-portal/syncs` — paginated sync history (status, counts, timestamps)
- `GET /api/admin-portal/syncs/{sync_id}` — sync detail with aggregate audit stats
- `GET /api/admin-portal/syncs/{sync_id}/audit` — paginated per-record audit entries (the transparency view)
- `GET /api/admin-portal/syncs/{sync_id}/audit/summary` — field-level summary (how many records had each field, completeness %)

**Data overview**:
- `GET /api/admin-portal/overview` — aggregate stats for the school's corpus:
  - Total alumni count
  - Distribution by graduation year
  - Major distribution (top N)
  - Career area distribution
  - Completeness: % with CIP codes, % with career outcomes (non-synthetic), % with pivots, avg courses per alumnus

**Audit and compliance**:
- `GET /api/admin-portal/audit/privacy` — privacy compliance summary: which PII field categories have been encountered across all syncs, confirmation all were discarded, total records processed
- `GET /api/admin-portal/audit/timeline` — chronological log of all admin actions and sync events for this school

**Operations**:
- `POST /api/admin-portal/recompute` — rebuild constellations for this school's students

### 7. Integration Points

- Include router in `app/main.py`
- Add to `app/config.py`: `jwt_secret_key`, `jwt_expiry_minutes`
- Add `PyJWT` to requirements

---

## Phase 2: NSC Pull Integration

- **NSC authorization flow**: school authorizes Constella as data recipient in NSC's system (out-of-band process, admin portal guides it step by step)
- **NscAdapter** (`app/ingest/sources/nsc.py`): authenticates with NSC, pulls via ETX (reuses PESC parsing utilities) or NextGen JSON API
- **SchoolConfig additions**: `nsc_org_id`, `nsc_api_key` (encrypted at rest), `nsc_authorized`
- **Scheduled syncs**: `SyncSchedule` model (school_id, frequency, next_run_at, enabled), background worker

## Phase 3: Career Outcomes + Advanced Audit

- Endpoint for schools to push employment/career survey data → `CareerOutcome` with `provenance='reported'`
- Data quality alerts (configurable thresholds, email notifications)
- Retention policies (auto-expire audit entries after N months)
- Export audit reports as PDF/CSV for compliance teams

---

## Verification

1. Parse sample PESC XML → verify no PII in AlumnusRecord output, stable anonymous IDs
2. Load same adapter twice → verify upsert (no duplicates), records_updated count correct
3. POST XML to /api/ingest/pesc/{slug} → verify SyncJob + IngestAuditEntry rows created
4. GET /audit endpoints → verify field-level transparency (fields_received vs fields_discarded)
5. GET /overview → verify aggregate stats match DB counts
6. End-to-end: push PESC XML → verify constellation updates for that school's students
