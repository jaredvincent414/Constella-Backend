"""MIDFIELD source adapter.

MIDFIELD (https://midfieldr.github.io/midfielddata/) ships anonymized registrar
records for ~98k students across four tables keyed by student id (`mcid`):

    student  demographics, one row per student
    term     program (major) + standing per student per term  -> pivots
    course   individual courses with grades per term           -> transcript
    degree   degrees earned                                     -> graduation

This adapter turns degree-earners into `AlumnusRecord`s and synthesizes a few
`StudentRecord`s from real pre-pivot transcripts.

Two things MIDFIELD does NOT contain, and how they're handled:

* **Career outcomes.** There is no employment data — the records stop at the
  degree. Constella clusters on `career_area`, so we substitute the academic
  discipline of the final degree (its CIP family). `outcome_title` is the degree
  name and `outcome_org` is the (anonymized) institution, both clearly academic
  rather than employment. When real outcome data arrives, a new adapter fills
  these fields for real and nothing else changes.
* **Interests / extracurriculars.** Not collected, so `interests` is empty and
  the 10% interest component contributes nothing. That is correct behaviour, not
  a bug — the scorer already treats an empty interest set as zero overlap.

Known modelling caveats (see README): course-code overlap is only meaningful
within one institution, since the three institutions number courses differently;
and MIDFIELD terms span 5+ years, which this adapter folds onto Constella's
Fall/Spring semester index.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import pandas as pd
import structlog

from app.ingest.cip import career_area_for_cip, clean_degree_name
from app.ingest.records import (
    AlumnusRecord,
    CourseRecord,
    MajorRecord,
    MilestoneRecord,
    PivotRecord,
    StudentRecord,
)
from app.models import StudentYear, semester_label

log = structlog.get_logger(__name__)

# MIDFIELD term codes are YYYYT; the trailing digit is the season. 1 (Fall) and
# 3 (Spring) are the primary terms that advance the academic clock; 4/5/6 are
# summer sessions that fold onto the preceding primary term.
PRIMARY_SEASONS = frozenset({"1", "3"})


# --------------------------------------------------------------------------
# Pure transform helpers (no I/O — unit-tested in tests/test_ingest_midfield.py)
# --------------------------------------------------------------------------


def build_semester_map(term_codes: list[str]) -> dict[str, int]:
    """Map each of a student's term codes to a 0-based semester index.

    Fall/Spring terms advance the counter; summer terms share the index of the
    primary term they follow. MIDFIELD students routinely exceed eight
    semesters, so indexes can run past 7 — the timeline builder clamps the
    *label* to Senior while `semester_index` comparisons (pre-pivot filtering)
    stay correct.
    """
    mapping: dict[str, int] = {}
    index = -1
    for term in sorted({t for t in term_codes if t}):
        if term[-1] in PRIMARY_SEASONS:
            index += 1
        mapping[term] = max(index, 0)
    return mapping


def is_dropped(grade: str | None) -> bool:
    """A withdrawal (W / WF / WP) is a dropped course; everything else counts."""
    return bool(grade) and grade.strip().upper().startswith("W")


def _grad_year(term_degree: str) -> int:
    try:
        return int(str(term_degree)[:4])
    except (ValueError, TypeError):
        return 0


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (ValueError, TypeError):
        return None


def transform_alumnus(
    mcid: str,
    degrees: list[tuple[str, str, str, str]],
    terms: list[tuple[str, str, str, str]],
    courses: list[tuple[str, str, str, str, str, str]],
    cip_name: dict[str, str],
) -> AlumnusRecord:
    """Build one `AlumnusRecord` from a student's raw MIDFIELD rows.

    Args are tuples so this stays trivially callable from tests:
        degrees: (cip6, term_degree, institution, degree_name)
        terms:   (term, cip6, level, coop)
        courses: (term, course_code, course_name, grade, discipline, hours)
    """

    def name_of(cip6: str) -> str:
        return cip_name.get(cip6) or career_area_for_cip(cip6)

    all_term_codes = [t[0] for t in terms] + [c[0] for c in courses]
    semester = build_semester_map(all_term_codes)

    # --- majors + pivots, walking the program sequence in term order --------
    declared: list[tuple[str, int]] = []  # (cip6, first semester index)
    pivots: list[PivotRecord] = []
    entered: set[str] = set()
    last_cip: str | None = None
    for term, cip6, _level, _coop in sorted(terms, key=lambda r: r[0]):
        if not cip6:
            continue
        si = semester.get(term, 0)
        if cip6 not in entered:
            entered.add(cip6)
            declared.append((cip6, si))
            # A change into a program not previously held is a pivot. Returning
            # to an earlier program is not counted again, which filters the
            # term-to-term oscillation present in real registrar data.
            if last_cip is not None and cip6 != last_cip:
                pivots.append(
                    PivotRecord(
                        semester_index=si,
                        from_major=name_of(last_cip),
                        to_major=name_of(cip6),
                        note=f"Changed program in {semester_label(si)}",
                    )
                )
        last_cip = cip6

    degree_cips = {d[0] for d in degrees}
    # role/provenance default to primary/reported: MIDFIELD reports one major per
    # term, all first-hand. Minors, if any, come from the derived-minor job later.
    majors = [
        MajorRecord(
            name=name_of(cip6), cip6=cip6, declared_semester=si, is_final=cip6 in degree_cips
        )
        for cip6, si in declared
    ]
    # Guarantee the awarded degree(s) appear as final majors even if the term
    # table never carried that program code.
    present = {m.name for m in majors}
    last_si = max(semester.values(), default=0)
    for cip6, _term_degree, _inst, _dname in degrees:
        nm = name_of(cip6)
        if nm not in present:
            majors.append(
                MajorRecord(name=nm, cip6=cip6, declared_semester=last_si, is_final=True)
            )
            present.add(nm)

    # --- transcript, de-duplicated by course code ---------------------------
    by_code: dict[str, CourseRecord] = {}
    for term, code, cname, grade, discipline, hours in sorted(courses, key=lambda r: r[0]):
        if not code:
            continue
        si = semester.get(term, 0)
        dropped = is_dropped(grade)
        credits = _to_float(hours)
        prev = by_code.get(code)
        if prev is None:
            by_code[code] = CourseRecord(
                code=code, name=cname, semester_index=si, dropped=dropped,
                discipline=discipline or None, credit_hours=credits,
            )
        elif prev.dropped and not dropped:
            # A later passing attempt supersedes an earlier withdrawal.
            by_code[code] = CourseRecord(
                code=code, name=cname, semester_index=si, dropped=False,
                discipline=discipline or None, credit_hours=credits,
            )

    # --- outcome (academic stand-in for career) -----------------------------
    primary = sorted(degrees, key=lambda d: d[1])[0]  # earliest degree
    primary_cip, primary_term, institution, primary_degree = primary
    outcome_title = cip_name.get(primary_cip) or clean_degree_name(primary_degree)

    # --- milestones ---------------------------------------------------------
    milestones: list[MilestoneRecord] = []
    coop_semesters = sorted(
        {semester.get(t, 0) for t, _c, _l, coop in terms if coop.strip().lower() == "yes"}
    )
    for si in coop_semesters:
        milestones.append(MilestoneRecord(semester_index=si, text="Co-op / internship term"))
    milestones.append(MilestoneRecord(semester_index=last_si, text=f"Graduated — {outcome_title}"))

    return AlumnusRecord(
        id=mcid,
        graduation_year=_grad_year(primary_term),
        outcome_title=outcome_title,
        outcome_org=institution,
        # MIDFIELD has no employer, so institution doubles as `outcome_org` here.
        # The tenant still travels in its own field — see AlumnusRecord.
        school_name=institution,
        career_area=career_area_for_cip(primary_cip),
        interests=[],
        courses=list(by_code.values()),
        majors=majors,
        pivots=pivots,
        milestones=milestones,
    )


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


class MidfieldAdapter:
    name = "midfield"

    def __init__(
        self,
        path: str,
        *,
        alumni_limit: int = 1500,
        student_count: int = 30,
        seed: int = 42,
        chunksize: int = 200_000,
    ) -> None:
        self.dir = Path(path)
        if not self.dir.exists():
            raise FileNotFoundError(
                f"MIDFIELD data directory not found: {self.dir}. Point --path / "
                f"the data_path setting at the folder holding degree.csv etc."
            )
        self.alumni_limit = alumni_limit
        self.student_count = student_count
        self.seed = seed
        self.chunksize = chunksize
        self._alumni: list[AlumnusRecord] | None = None

    # -- file reading -----------------------------------------------------

    def _read(self, table: str, usecols: list[str], mcids: set[str] | None) -> pd.DataFrame:
        """Read a table (parquet if present and readable, else CSV in chunks),
        filtered to `mcids`. Everything is read as string to preserve CIP and
        term leading zeros; blanks stay empty strings rather than NaN."""
        parquet = self.dir / f"{table}.parquet"
        if parquet.exists():
            try:
                df = pd.read_parquet(parquet, columns=usecols)
                df = df.astype(str)
                if mcids is not None:
                    df = df[df["mcid"].isin(mcids)]
                return df.reset_index(drop=True)
            except (ImportError, ValueError):
                pass  # no parquet engine installed — fall back to CSV

        csv = self.dir / f"{table}.csv"
        if not csv.exists():
            raise FileNotFoundError(f"Neither {parquet.name} nor {csv.name} in {self.dir}")

        frames: list[pd.DataFrame] = []
        for chunk in pd.read_csv(
            csv, usecols=usecols, dtype=str, keep_default_na=False, chunksize=self.chunksize
        ):
            if mcids is not None:
                chunk = chunk[chunk["mcid"].isin(mcids)]
            if len(chunk):
                frames.append(chunk)
        if frames:
            return pd.concat(frames, ignore_index=True)[usecols]
        return pd.DataFrame(columns=usecols)

    # -- build ------------------------------------------------------------

    def _build_alumni(self) -> list[AlumnusRecord]:
        log.info("midfield_read_degrees", dir=str(self.dir))
        degree_cols = ["mcid", "term_degree", "cip6", "institution", "degree"]
        degree_df = self._read("degree", degree_cols, None)

        cip_name: dict[str, str] = {}
        for cip6, degree in zip(degree_df["cip6"], degree_df["degree"], strict=False):
            if cip6 and cip6 not in cip_name:
                cip_name[cip6] = clean_degree_name(degree)

        all_ids = sorted(set(degree_df["mcid"]))
        sample = all_ids if self.alumni_limit <= 0 else all_ids[: self.alumni_limit]
        sample_set = set(sample)
        log.info("midfield_sample", degree_earners=len(all_ids), sampled=len(sample_set))

        degrees_by: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
        for r in degree_df.itertuples(index=False):
            if r.mcid in sample_set:
                degrees_by[r.mcid].append((r.cip6, r.term_degree, r.institution, r.degree))

        term_df = self._read("term", ["mcid", "term", "cip6", "level", "coop"], sample_set)
        terms_by: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
        for r in term_df.itertuples(index=False):
            terms_by[r.mcid].append((r.term, r.cip6, r.level, r.coop))

        course_cols = [
            "mcid", "term_course", "abbrev", "number", "course",
            "grade", "discipline_midfield", "hours_course",
        ]
        course_df = self._read("course", course_cols, sample_set)
        courses_by: dict[str, list[tuple[str, str, str, str, str, str]]] = defaultdict(list)
        for r in course_df.itertuples(index=False):
            code = f"{r.abbrev}{r.number}".strip()
            courses_by[r.mcid].append(
                (r.term_course, code, r.course, r.grade, r.discipline_midfield, r.hours_course)
            )

        alumni = [
            transform_alumnus(
                mcid, degrees_by[mcid], terms_by.get(mcid, []), courses_by.get(mcid, []), cip_name
            )
            for mcid in sample
        ]
        log.info("midfield_built_alumni", count=len(alumni))
        return alumni

    def _get_alumni(self) -> list[AlumnusRecord]:
        if self._alumni is None:
            self._alumni = self._build_alumni()
        return self._alumni

    # -- SourceAdapter interface -----------------------------------------

    def alumni(self) -> Iterator[AlumnusRecord]:
        yield from self._get_alumni()

    def students(self) -> Iterator[StudentRecord]:
        """Synthesize current students from real pre-pivot transcripts.

        Each chosen alumnus becomes a student standing where they once stood:
        their declared (origin) major, the courses they'd taken before the
        pivot, and the year the pivot happened. This is a validation
        convenience — real current students come from the application, not from
        this dataset — so ids are namespaced `mid-student-*`.
        """
        pivoted = [a for a in self._get_alumni() if a.pivots]
        for alum in pivoted[: self.student_count]:
            pivot = min(alum.pivots, key=lambda p: p.semester_index)
            year = StudentYear.from_index(pivot.semester_index // 2).value
            pre_courses = [
                CourseRecord(code=c.code, name=c.name, semester_index=c.semester_index)
                for c in alum.courses
                if c.semester_index < pivot.semester_index and not c.dropped
            ]
            yield StudentRecord(
                id=f"mid-student-{alum.id}",
                year=year,
                # Stand where they stood, at the school they attended — otherwise
                # the demo student's own cohort would be invisible to them.
                school_name=alum.school_name,
                declared_major=pivot.from_major,
                intended_direction=None,
                interests=[],
                courses=pre_courses,
            )
