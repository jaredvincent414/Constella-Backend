# Frontend API Update Handoff

Backend branch `master` at commit `302e1fd`. All response fields are camelCase on the wire.

---

## 1. Breaking: Student Profile (`GET /api/students/me`)

### Response shape changed (`StudentOut`)

| Old field | New field | Notes |
|-----------|-----------|-------|
| `name` | `firstName`, `lastName` | Split from the single `name` column. Both nullable. |
| `schoolName` | `school` | Display name of the school |
| `year` | `currentYear` | Same enum values (freshman, sophomore, etc.) |
| `major` | `declaredMajor` | Primary major from program table |
| — | `minors` | New: `string[]` |
| `courses` | `coursesCompleted` | Array of `StudentCourseOut` (see below) |
| — | `schoolId` | New: the slug identifier for the student's school |

### Course items (`coursesCompleted[]`)

| Old field | New field | Notes |
|-----------|-----------|-------|
| `code` | `id` | The course code. Round-trips as `code` on write. |
| — | `name` | Course display name |
| — | `semester` | Label like "Sophomore Spring" |
| `semesterIndex` | `semesterIndex` | Unchanged, 0–7 |

### Action required
- Update all reads of the profile response to use the new field names.
- Key course lists on `id` instead of `code`.

---

## 2. Breaking: Registration (`POST /api/students/register`)

### Request body changed (`RegisterRequest`)

| Old field | New field | Notes |
|-----------|-----------|-------|
| `name` | `firstName`, `lastName` | Both optional, max 80 chars |
| — | `password` | **Required**, min 10 chars, max 256 |
| `schoolId` | `schoolId` | Unchanged, still required |
| `email` | `email` | Unchanged |
| `year` | `year` | Unchanged, defaults to "freshman" |

### Response unchanged in structure
Returns `{ token, student }` where `student` uses the new `StudentOut` shape above.

### Action required
- Signup form must collect `password`.
- Send `firstName`/`lastName` instead of `name`.

---

## 3. New: Login (`POST /api/students/login`)

### Request body

```json
{
  "email": "student@example.edu",
  "password": "their-password"
}
```

### Response (same shape as register)

```json
{
  "token": "bearer-token-string",
  "student": { /* StudentOut */ }
}
```

### Error responses
- `401` — wrong credentials (same body regardless of reason)
- `429` — too many failed attempts (throttled per email + IP, window resets after configurable TTL)

### Behavioral note
Login **rotates the token** — the old token stops working (within ~60s due to auth cache TTL). Only one active session per student.

---

## 4. Breaking: Constellation (`GET /api/constellation`)

### New optional query parameters

| Param | Type | Description |
|-------|------|-------------|
| `interests` | string | Comma-separated, e.g. `Biology,Psychology`. Filters corpus. |
| `careerArea` | string | Filter by career area label |
| `major` | string | Filter alumni who started or graduated in this major |
| `maxAlumni` | int | Cap on returned alumni (1–1000) |
| `refresh` | bool | Bypass cache, force recompute |
| `toMajor` | string | Existing — target major |
| `fromMajor` | string | Existing — origin major |

### Response structure changed

**Before (flat):**
```json
{
  "student": {...},
  "alumni": [{ "id": "...", "clusterId": "...", "similarity": 0.85, ... }],
  "clusters": [{ "id": "...", "label": "...", ... }]
}
```

**After (nested):**
```json
{
  "student": { "year": "sophomore", "interests": [...], "courses": [...] },
  "clusters": [
    {
      "id": "cluster-uuid",
      "label": "Health Policy",
      "similarity": 0.72,
      "topMajors": ["Biology", "Public Health"],
      "alumni": [
        {
          "id": "alumnus-uuid",
          "similarityScore": 85.0,
          "graduationYear": 2022,
          "cluster": "Health Policy",
          "matchReason": "Strong course overlap in biology and shared pre-med interests",
          "majors": ["Biology"],
          "minors": ["Chemistry"],
          "programs": [{ "code": "Biology", "role": "major", "provenance": "reported" }],
          "careerOutcome": { "title": "Analyst", "org": "CDC", "industry": "...", "provenance": "synthetic" },
          "interests": ["Research", "Public Health"],
          "pivotPoints": [{ "semester": "Junior Fall", "fromMajor": "Chemistry", "toMajor": "Biology" }]
        }
      ]
    }
  ],
  "clusterEdges": [{ "source": "id-a", "target": "id-b", "weight": 0.4 }],
  "totalAlumni": 50,
  "summary": "50 alumni across 6 clusters",
  "meta": {
    "cached": true,
    "generatedAt": "2026-08-20T14:30:00Z",
    "totalCandidates": 240,
    "returned": 50,
    "edgesBeforePruning": 12
  }
}
```

### Key field changes on alumni nodes

| Old | New | Notes |
|-----|-----|-------|
| `similarity` (0–1) | `similarityScore` (0–100) | Now a percentage |
| `classYear` | `graduationYear` | Same int value |
| `clusterId` | `cluster` | Now the cluster's **label** string, not a UUID (alumni are nested under their cluster) |
| `outcome` | `careerOutcome` | Now `OutcomeOut` object: `{ title, org, industry?, occupation?, region?, provenance? }` |
| — | `matchReason` | Nullable one-line string for tooltip |
| — | `minors` | `string[]` |
| — | `interests` | `string[]` |
| — | `pivotPoints` | `[{ semester, fromMajor, toMajor }]` |
| — | `programs` | `[{ code, role, provenance }]` — full picture with provenance |

### Action required
- Iterate `clusters[].alumni` instead of a top-level `alumni[]`.
- Use `similarityScore` (0–100) for node sizing / labels.
- Update outcome rendering to use the structured `careerOutcome` object.
- Check `provenance` on `careerOutcome` — if `"synthetic"`, do not present as real.

---

## 5. Breaking: What-If Simulator (`POST /api/simulate`)

### Request unchanged
```json
{ "toMajor": "Computer Science", "fromMajor": "Biology", "topN": 5 }
```

### Response completely reshaped

**Before:**
```json
{
  "student": {...},
  "totalCandidates": 12,
  "matches": [{ "id": "...", "similarity": 0.9, ... }]
}
```

**After:**
```json
{
  "fromMajor": "Biology",
  "toMajor": "Computer Science",
  "totalTransitions": 12,
  "peakTiming": "sophomore and junior year",
  "topOutcome": "Software Engineering",
  "topOutcomeCount": 5,
  "cards": [
    {
      "id": "alumnus-uuid",
      "isTopMatch": true,
      "classYear": 2021,
      "matchPercent": 92.0,
      "fromMajor": "Biology",
      "toMajor": "Computer Science",
      "pivotSemester": "Junior Fall",
      "pivotType": "switched",
      "careerOutcome": { "title": "SWE", "org": "Google", "provenance": "synthetic" },
      "prePivotSummary": "3 shared courses before pivot",
      "timeline": [
        {
          "semester": "Sophomore Spring",
          "pivot": false,
          "courses": [{ "name": "Intro to CS", "tag": "new" }]
        },
        {
          "semester": "Junior Fall",
          "pivot": true,
          "courses": [{ "name": "Data Structures", "tag": "new" }]
        }
      ]
    }
  ]
}
```

### Key changes

| Old | New | Notes |
|-----|-----|-------|
| `matches[]` | `cards[]` | Completely new shape (`TransitionCard`) |
| `totalCandidates` | `totalTransitions` | Renamed |
| `student` | removed | No longer in response |
| — | `peakTiming` | Nullable string, e.g. "sophomore and junior year" |
| — | `topOutcome` | Most common destination industry (nullable) |
| — | `topOutcomeCount` | Count for the above |
| — | `fromMajor` | Echo of the query |

### Card fields (`cards[]`)

| Field | Type | Notes |
|-------|------|-------|
| `id` | string | Same alumnus ID as constellation nodes — opens detail panel |
| `isTopMatch` | bool | Highlight flag |
| `classYear` | int | Graduation year |
| `matchPercent` | float 0–100 | Match strength |
| `fromMajor` | string? | What they started in |
| `toMajor` | string? | What they ended in |
| `pivotSemester` | string? | e.g. "Junior Fall" |
| `pivotType` | string? | `"switched"` / `"added"` / `"dropped"` |
| `careerOutcome` | OutcomeOut | Same structured object as constellation |
| `prePivotSummary` | string | e.g. "3 shared courses before pivot" |
| `timeline` | TransitionSemester[] | Mini timeline for the card |

### Action required
- Replace `matches` iteration with `cards`.
- Render the new header stats (`peakTiming`, `topOutcome`, `topOutcomeCount`).
- Use `pivotType` to distinguish "switched majors" from "added a second major".
- `timeline[].courses[].tag` uses `"kept"` / `"new"` / `"dropped"` (same vocab as detail panel).

---

## 6. Breaking: Health Check (`GET /api/health/ready`)

- Now returns **503** when Postgres is unreachable (was 200 with status field).
- If used for load balancer checks, this is now standard behavior (200 = healthy, 503 = unhealthy).

---

## 7. New Endpoints (Additive)

### `GET /api/students/me/dashboard`

```json
{
  "stats": {
    "alumniMatches": 50,
    "clusters": 6,
    "highestMatch": 92.0,
    "savedPaths": 3
  },
  "topMatches": [ /* AlumnusOut[] — same shape as constellation alumni */ ],
  "meta": { "cached": true, "generatedAt": "..." }
}
```

### `GET /api/students/me/activity?limit=20`

Returns recent activity (default limit 20):
```json
[
  {
    "id": 1,
    "kind": "explored",
    "label": "Explored Health Policy cluster",
    "at": "2026-08-20T14:30:00Z"
  }
]
```

Kind values: `"explored"`, `"saved_path"`, `"removed_path"`, `"combined_paths"`, `"simulated"`, `"updated_profile"`.

### `GET /api/search?q=bio`

Topbar typeahead:
```json
{
  "query": "bio",
  "results": [
    {
      "type": "major",
      "id": "biology",
      "label": "Biology",
      "detail": null,
      "count": 24,
      "provenance": null
    },
    {
      "type": "alumnus",
      "id": "alumnus-uuid",
      "label": "Class of 2022",
      "detail": "Biology → Health Policy",
      "count": null,
      "provenance": "synthetic"
    }
  ],
  "total": 2
}
```

Type values: `"major"`, `"cluster"`, `"alumnus"`. If `provenance` is `"synthetic"`, flag it in UI.

---

## 8. Behavioral: Caching & ETags

All cached responses now include:
- `ETag` header (content hash)
- `Cache-Control: private, no-cache` (store but always revalidate)

Frontend should send `If-None-Match` with the stored ETag on subsequent requests. The backend will return **304 Not Modified** with no body if nothing changed. This saves bandwidth significantly on constellation payloads.

Responses are gzip-compressed by the backend. Do **not** add client-side gzip middleware that would double-compress.

---

## 9. Alumni Detail Timeline (unchanged URL, minor additions)

`GET /api/alumni/{id}/timeline` — no URL or auth change.

New optional field in response:
- `matchReason`: `{ summary: string, factors: string[] } | null`

The `factors` array contains individual reason chips (e.g., "12 shared courses", "Same starting major").

---

## Summary of Required Frontend Changes

1. **Auth/signup flow** — add password field, split name into firstName/lastName, add login endpoint
2. **Profile display** — update all field names per the mapping table
3. **Constellation rendering** — iterate nested `clusters[].alumni`, use `similarityScore` (0–100), structured `careerOutcome`
4. **What-If page** — completely new response shape with `cards[]` and header stats
5. **ETag support** — send `If-None-Match` for 304 optimization
6. **New pages** — dashboard, activity feed, search (all additive, implement when ready)
