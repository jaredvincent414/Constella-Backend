# Frontend Feature Update — Backend Handoff

**Date:** August 17, 2026

What the frontend has built, what API endpoints it expects, and the exact data shapes each feature needs from the backend.

---

## API Endpoints the Frontend Expects

The frontend API client lives at `app/src/lib/api.ts`. Base URL defaults to `http://localhost:8000`.

### `POST /auth/login`

**Request:**
```json
{ "email": "string", "password": "string" }
```

**Response:**
```json
{ "token": "string" }
```

**Used by:** Login page. Token is stored client-side and sent as `Authorization: Bearer <token>` on all protected routes.

---

### `POST /auth/signup`

**Request:**
```json
{
  "firstName": "string",
  "lastName": "string",
  "email": "string",
  "password": "string"
}
```

**Response:**
```json
{ "token": "string" }
```

**Used by:** Signup page. Frontend also has a Google OAuth button — no backend wiring for this yet.

---

### `GET /me`

**Headers:** `Authorization: Bearer <token>`

**Response:** `StudentProfile`
```json
{
  "id": "string",
  "firstName": "string",
  "lastName": "string",
  "email": "string",
  "school": "string",
  "currentYear": "string",
  "declaredMajor": "string | null",
  "coursesCompleted": [
    { "id": "string", "name": "string", "semester": "string" }
  ],
  "interests": ["string"]
}
```

**Used by:** Dashboard (greeting, stats), sidebar (user profile section).

---

### `GET /explore`

**Headers:** `Authorization: Bearer <token>`

**Query params (all optional):**
- `interests` — comma-separated list (e.g. `Biology,Psychology`)
- `career_area` — string (e.g. `Health Policy`)
- `major` — string (e.g. `Biochemistry`)

**Response:** `ConstellationData`
```json
{
  "clusters": [
    {
      "id": "string",
      "label": "string",
      "alumni": [
        {
          "id": "string",
          "graduationYear": 2022,
          "majors": ["Biochemistry", "Public Health"],
          "minors": ["string"] ,
          "careerOutcome": {
            "title": "Health Policy Analyst",
            "industry": "Health Policy"
          },
          "coursesBySemester": {
            "Freshman Fall": [
              { "id": "string", "name": "Bio 101", "semester": "Freshman Fall", "status": "kept" }
            ]
          },
          "interests": ["biology", "public health"],
          "pivotPoints": [
            { "semester": "Junior Fall", "fromMajor": "Biology", "toMajor": "Public Health" }
          ],
          "similarityScore": 92.5,
          "cluster": "Health Policy"
        }
      ],
      "topMajors": ["Biochemistry", "Public Health", "Biology"],
      "x": 0.28,
      "y": -0.15
    }
  ],
  "totalAlumni": 50,
  "summary": "50 alumni across 6 career clusters"
}
```

**Used by:** Explore page. Two modes hit the same endpoint:
- **Open mode** — may send `interests`, `career_area`, and/or `major`
- **Focused mode** — sends only `career_area`

The frontend renders `clusters` as a D3 force-directed constellation. Each alumni node is clickable and opens a detail panel showing `coursesBySemester`, `pivotPoints`, `careerOutcome`, and `interests`.

**Important fields for the detail panel:**
- `coursesBySemester` — the full semester-by-semester course list. Each course has a `status` field (`"kept"`, `"new"`, `"dropped"`) that controls how the course pill renders (neutral, highlighted, or strikethrough).
- `pivotPoints` — marks which semester was a pivot. The timeline renders pivot semesters with a diamond node and indigo highlight.
- `similarityScore` — displayed as match percentage and used to size the alumni node on the graph.

---

### `POST /simulate`

**Headers:** `Authorization: Bearer <token>`

**Request:** `SimulationQuery`
```json
{
  "fromMajor": "Economics",
  "toMajor": "Public Health"
}
```

**Response:** The frontend currently expects `SimulationResult` from the API client:
```json
{
  "constellation": { "...ConstellationData" },
  "summary": "string",
  "totalTransitions": 12,
  "peakPivotTiming": "sophomore and junior year"
}
```

However, the Transition page actually renders a **different shape** — `TransitionSimulation`. This is the shape the page component works with directly:
```json
{
  "totalTransitions": 12,
  "peakTiming": "sophomore and junior year",
  "topOutcome": "Public Health",
  "topOutcomeCount": 5,
  "cards": [
    {
      "isTopMatch": true,
      "classYear": 2024,
      "matchPercent": 94,
      "fromMajor": "Economics",
      "toMajor": "Public Health",
      "outcome": "Public Health Researcher @ WHO",
      "prePivotSummary": "Pre-pivot (3 semesters): Economics 101, Stats, Intro Economics",
      "timeline": [
        {
          "semester": "Sophomore Spring",
          "pivot": true,
          "courses": [
            { "name": "Intro Public Health", "tag": "new" },
            { "name": "Public Health Methods", "tag": "new" },
            { "name": "Adv. Economics", "tag": "dropped" }
          ]
        },
        {
          "semester": "Junior Spring",
          "pivot": false,
          "courses": [
            { "name": "Public Health Seminar", "tag": "new" },
            { "name": "Applied Public Health", "tag": null }
          ]
        }
      ]
    }
  ]
}
```

**Note:** There's a mismatch between what `api.ts` types as the response (`SimulationResult`) and what the Transition page actually consumes (`TransitionSimulation`). The backend should return the `TransitionSimulation` shape. The `cards` array is what gets rendered — each card is a full alumni transition story with semester-by-semester timeline.

**Used by:** Transition page. Student enters "from" and "to" majors, clicks Simulate, sees summary bar + ranked transition cards.

---

## Endpoints Not Yet Defined (Frontend Needs)

These features exist in the frontend but have no API client function yet.

### Dashboard stats

The dashboard shows 4 stats: Alumni Matches, Clusters Explored, Highest Match, Saved Paths. Currently hardcoded. Needs either a dedicated `GET /dashboard/stats` endpoint or can be derived from `/me` + `/explore` responses.

### Dashboard top matches + recent activity

- **Top matches**: 4 alumni paths with match %, major, outcome. Could come from a `GET /matches?limit=4` or be part of the dashboard stats response.
- **Recent activity**: timestamped list of user actions (e.g. "Explored Health Policy cluster", "Saved a new path"). Needs a `GET /activity?limit=4` or similar.

### Saved paths

The Create Path page displays a list of saved alumni paths. Currently hardcoded as `MOCK_SAVED_PATHS`. Each saved path has:

```json
{
  "id": 1,
  "match": 94,
  "color": 0,
  "major": "Biochem + Public Health",
  "outcome": "Health Policy Analyst @ State Dept",
  "classYear": 2022,
  "courses": ["Bio 101", "Chem 101", "Organic Chem", "..."],
  "semesters": {
    "Freshman": ["Bio 101", "Chem 101", "Intro Psych"],
    "Sophomore": ["Organic Chem", "Physics", "Intro Public Health"],
    "Junior": ["Epidemiology", "Health Policy", "Biostatistics"],
    "Senior": ["Community Health", "Capstone: Health Equity"]
  },
  "outcomeField": "Health Policy"
}
```

Needs:
- `GET /saved-paths` — list user's saved paths
- `POST /saved-paths` — save an alumni path (triggered from Explore page detail panel)
- `DELETE /saved-paths/:id` — unsave a path

The Explore page currently saves paths to `localStorage` under key `constella-saved-paths`. This should move to the backend.

### Search

The topbar has a search input ("Search paths, majors..."). No functionality yet. Would need a `GET /search?q=...` endpoint returning mixed results (alumni, majors, clusters).

---

## Data the Backend Must Provide

### Per-alumni record (core data unit)

Every feature depends on alumni records. The minimum fields needed across all features:

| Field | Type | Used by |
|-------|------|---------|
| `id` | string | All — unique identifier |
| `graduationYear` | number | Explore detail panel, transition cards |
| `majors` | string[] | Explore (filtering, display), transition cards |
| `minors` | string[] (optional) | Explore detail panel |
| `careerOutcome.title` | string | Explore detail, dashboard matches, transition cards, saved paths |
| `careerOutcome.industry` | string | Explore (clustering), transition cards |
| `coursesBySemester` | Record<string, Course[]> | Explore detail panel timeline, Create Path Sankey diagram |
| `interests` | string[] | Explore detail panel, matching |
| `pivotPoints` | PivotPoint[] (optional) | Explore detail panel (diamond node rendering) |
| `similarityScore` | number (0-100) | Explore (node size + match %), dashboard matches, transition cards |
| `cluster` | string | Explore (constellation coloring + grouping) |

### Course object

```json
{
  "id": "string",
  "name": "string",
  "semester": "string",
  "status": "kept | new | dropped"
}
```

`status` is critical for the timeline renderers — it controls visual treatment of each course pill.

### Cluster object

```json
{
  "id": "string",
  "label": "string",
  "alumni": [AlumniRecord],
  "topMajors": ["string"],
  "x": 0.28,
  "y": -0.15
}
```

`x` and `y` are normalized positions (roughly -0.5 to 0.5) used as initial cluster positions in the D3 force layout. The frontend multiplies these by canvas dimensions. The backend can compute these or the frontend can derive them — either works.

---

## Feature Summary

| Feature | Page | Backend data needed | Endpoint |
|---------|------|-------------------|----------|
| Auth (login/signup) | `/login`, `/signup` | Email/password validation, JWT token | `POST /auth/login`, `POST /auth/signup` |
| Student profile | Dashboard, sidebar | Profile fields, courses, interests | `GET /me` |
| Constellation explore | `/explore` | Clustered alumni with similarity scores | `GET /explore` |
| Alumni detail | `/explore` (panel) | Full alumni record with coursesBySemester, pivotPoints | Included in `/explore` response |
| What-If simulation | `/transition` | Transition cards with timelines, summary stats | `POST /simulate` |
| Dashboard stats | `/dashboard` | Match count, clusters explored, highest match, saved count | TBD |
| Top matches | `/dashboard` | Top N alumni by similarity | TBD |
| Recent activity | `/dashboard` | User action log with timestamps | TBD |
| Saved paths | `/create-path` | CRUD for saved alumni paths with semester breakdowns | TBD |
| Search | Topbar | Full-text search across alumni, majors, clusters | TBD |
