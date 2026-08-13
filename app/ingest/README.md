# Data ingestion

A swappable boundary between a **dataset** and Constella's **corpus**.

```
source adapter (dataset-specific)  ->  source-neutral records  ->  loader  ->  Postgres
     sources/midfield.py                    records.py            loader.py     (ORM tables)
```

The matching engine, cache, and API only ever read the ORM tables the loader
fills. They never learn which dataset produced them. So switching datasets means
writing **one adapter** and flipping **one setting** — nothing downstream
changes.

## The contract

A source adapter implements `SourceAdapter` (`base.py`): two generators that
yield source-neutral dataclasses from `records.py`.

```python
class SourceAdapter(Protocol):
    name: str
    def alumni(self) -> Iterator[AlumnusRecord]: ...
    def students(self) -> Iterator[StudentRecord]: ...
```

`records.py` mirrors the ORM but has no SQLAlchemy, no I/O — so each adapter's
transform logic is unit-testable without a database (see
`tests/test_ingest_midfield.py`).

## Current source: MIDFIELD (practice data)

`sources/midfield.py` maps the four MIDFIELD tables into records:

| Constella field | MIDFIELD source |
|---|---|
| alumnus = one graduate | rows in `degree` |
| courses by semester | `course` (name, code = `abbrev`+`number`) |
| course `dropped` | grade starting with `W` (withdrawal) |
| pivots (when / from / to) | changes in `term.cip6` across terms |
| declared / final majors | `term.cip6` sequence + `degree.cip6` |
| graduation year | `degree.term_degree` |
| `career_area` (cluster axis) | **CIP 2-digit family** of the final degree |
| `outcome_title` / `outcome_org` | degree name / (anonymized) institution |
| interests | *none — empty* |

### Two deliberate stand-ins

MIDFIELD has **no employment data** and **no interests**. So:

- **Clustering runs on academic discipline**, not career outcome. `career_area`
  is the degree's CIP family (e.g. "Engineering", "Business & Management").
  `outcome_title`/`outcome_org` are the degree and institution — clearly
  academic, not a job. This is honest: it exercises the whole pipeline on real
  trajectories without inventing careers.
- **The 10% interest component contributes nothing** (empty set → zero overlap),
  which the scorer already handles correctly.

### Caveats to keep in mind while validating

- **Course-code overlap is within-institution only.** MIDFIELD's three schools
  number courses differently, so codes rarely match across institutions. The
  synthesized students are institution-matched to their source alum, so overlap
  is meaningful for them.
- **Cluster edges are sparse.** Edges are Jaccard over clusters' *major* sets;
  distinct disciplines share few majors, so few edges clear the threshold. With
  real career-area clustering (where several career clusters can share a major)
  edges reappear.
- **Terms span 5+ years.** MIDFIELD students exceed eight semesters;
  `build_semester_map` folds terms onto a Fall/Spring index (summers share the
  preceding term). `semester_index` can exceed 7; the timeline clamps the label
  to Senior but pre-pivot comparisons stay correct.

## Usage

```bash
# Preview the transform without touching the DB (fast, no Postgres needed):
python -m app.ingest --source midfield --dry-run --alumni 300

# Load a validation slice, replacing any existing corpus:
python -m app.ingest --source midfield --alumni 1500 --students 30 --reset

# Load the full corpus (~50k alumni):
python -m app.ingest --source midfield --alumni 0 --reset

# Then rebuild the cached constellations:
python -m app.jobs.recompute
```

Defaults come from `app.config` (`DATA_SOURCE`, `DATA_PATH`,
`INGEST_ALUMNI_LIMIT`, `INGEST_STUDENT_COUNT`, `INGEST_SEED`), so a bare
`python -m app.ingest --reset` uses the configured source.

## Swapping in real school data

1. Copy `sources/template.py` to `sources/<your_source>.py` and implement
   `alumni()` and `students()` — read the school's export, emit the record
   dataclasses. Fill `career_area` / `outcome_title` / `outcome_org` with
   **real employment data**; clustering then becomes career-outcome clustering
   as the spec intends.
2. Register it in `sources/__init__.py`: add one line to `_REGISTRY`.
3. Point the config at it: `DATA_SOURCE=<name>`, `DATA_PATH=<dir>` (or
   `--source <name> --path <dir>`).
4. `python -m app.ingest --reset` then `python -m app.jobs.recompute`.

No changes to the loader, models, scorer, clustering, cache, or API.
```
