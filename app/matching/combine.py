"""Path combining — merge several saved alumni paths into one ideal plan.

The student picks 2+ saved paths on the Create Path page; this merges them into a
single semester-by-semester plan plus the pre-computed Sankey the frontend draws.
Pure and session-free like the rest of the matching engine: it takes loaded ORM
objects and returns the wire schema.

Approach (from the spec):
  1. bucket each path's courses into the four year-stages
  2. rank within a stage by cross-path frequency (shared = consensus)
  3. cap per stage, tie-breaking on relevance to the student's interests
  4. confidence = shared / total-unique courses across the selected paths

Two honest simplifications for the placeholder data:
  * No prerequisite graph exists, so "resolve prereq-chain conflicts" reduces to
    de-duplicating the same course (by normalized code) — which is the practical
    case anyway.
  * Course identity is the normalized code, so overlap only registers within an
    institution (MIDFIELD numbers courses differently across its three schools).
    That depresses `confidence` on cross-institution combinations — expected.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from app.config import settings
from app.matching.programs import program_views
from app.matching.scoring import StudentProfile
from app.matching.text import normalize_course_code, tokenize
from app.models import SEMESTERS_PER_YEAR, YEAR_LABELS, Alumnus
from app.schemas.paths import (
    CombinedPath,
    CombineResponse,
    SankeyData,
    SankeyLink,
    SankeyNode,
    SourcePath,
)

# The Sankey row that holds outcome-field nodes, one past the last year-stage.
OUTCOME_STAGE = len(YEAR_LABELS)


def _stage(semester_index: int) -> int:
    return min(semester_index // SEMESTERS_PER_YEAR, len(YEAR_LABELS) - 1)


def _courses_by_stage(alumnus: Alumnus) -> dict[int, dict[str, str]]:
    """stage -> {normalized_code: display_name} for non-dropped courses."""
    by_stage: dict[int, dict[str, str]] = defaultdict(dict)
    for course in alumnus.courses:
        # Skip dropped courses and rows the source left without a title — an
        # untitled course can't appear in a plan the student is meant to read.
        if course.dropped or not course.course_name.strip():
            continue
        by_stage[_stage(course.semester_index)][normalize_course_code(course.course_code)] = (
            course.course_name.strip()
        )
    return by_stage


def _relevance_scorer(profile: StudentProfile | None):
    """A cheap tiebreaker: how many of a course name's tokens the student's
    interests / intended direction / majors touch."""
    if profile is None:
        return lambda _name: 0
    tokens: set[str] = set()
    for text in [*profile.interests, profile.intended_direction or "", *profile.major_set]:
        tokens |= tokenize(text)
    return lambda name: len(tokenize(name) & tokens)


def combine_paths(
    profile: StudentProfile | None,
    alumni: list[Alumnus],
) -> CombineResponse:
    """Merge `alumni` (in selection order — index is the color) into one plan."""
    paths = [_courses_by_stage(a) for a in alumni]

    # Canonical display name per course code (first seen wins).
    name_of: dict[str, str] = {}
    for path in paths:
        for courses in path.values():
            for code, name in courses.items():
                name_of.setdefault(code, name)

    # Frequency of each course within a stage, and the set of paths holding it.
    stage_freq: Counter[tuple[int, str]] = Counter()
    code_paths: dict[str, set[int]] = defaultdict(set)
    for i, path in enumerate(paths):
        for stage, courses in path.items():
            for code in courses:
                stage_freq[(stage, code)] += 1
                code_paths[code].add(i)

    # --- combined plan: top-N per stage by frequency, then relevance --------
    relevance = _relevance_scorer(profile)
    cap = settings.combine_max_courses_per_stage
    semesters: dict[str, list[str]] = {}
    for stage in range(len(YEAR_LABELS)):
        codes = [code for (s, code) in stage_freq if s == stage]
        codes.sort(key=lambda c: (-stage_freq[(stage, c)], -relevance(name_of[c]), name_of[c]))
        semesters[YEAR_LABELS[stage]] = [name_of[c] for c in codes[:cap]]

    # --- shared courses + confidence ----------------------------------------
    shared_codes = {code for code, ps in code_paths.items() if len(ps) >= 2}
    shared_courses = sorted(name_of[c] for c in shared_codes)
    total_unique = len(code_paths)
    confidence = round(len(shared_codes) / total_unique, 2) if total_unique else 0.0

    outcome_fields = sorted({a.career_area for a in alumni})

    source_paths = [
        SourcePath(
            alumnus_id=a.id,
            # Readable major names (not cip codes), falling back to the outcome.
            major=" + ".join(p.code for p in program_views(a) if p.role == "major")
            or a.career_area,
            color_index=i,
        )
        for i, a in enumerate(alumni)
    ]

    sankey = _build_sankey(paths, alumni, name_of, stage_freq)

    return CombineResponse(
        combined_path=CombinedPath(
            semesters=semesters,
            outcome_fields=outcome_fields,
            confidence=confidence,
        ),
        source_paths=source_paths,
        shared_courses=shared_courses,
        sankey=sankey,
    )


def _build_sankey(
    paths: list[dict[int, dict[str, str]]],
    alumni: list[Alumnus],
    name_of: dict[str, str],
    stage_freq: Counter[tuple[int, str]],
) -> SankeyData:
    """One representative course per path per stage (the most-shared one), one
    link per path per stage transition, and a final link into the outcome node.
    Every link endpoint is a declared node, so the graph is always drawable."""
    # rep[(path_index, stage)] = code
    rep: dict[tuple[int, int], str] = {}
    for i, path in enumerate(paths):
        for stage, courses in path.items():
            rep[(i, stage)] = min(
                courses, key=lambda c: (-stage_freq[(stage, c)], name_of[c])
            )

    node_paths: dict[tuple[int, str], set[int]] = defaultdict(set)
    for (i, stage), code in rep.items():
        node_paths[(stage, name_of[code])].add(i)

    outcome_paths: dict[str, set[int]] = defaultdict(set)
    for i, a in enumerate(alumni):
        outcome_paths[a.career_area].add(i)

    nodes = [
        SankeyNode(id=f"{stage}-{label}", stage=stage, label=label, path_ids=sorted(pids))
        for (stage, label), pids in sorted(node_paths.items())
    ]
    nodes += [
        SankeyNode(
            id=f"{OUTCOME_STAGE}-{field}", stage=OUTCOME_STAGE, label=field, path_ids=sorted(pids)
        )
        for field, pids in sorted(outcome_paths.items())
    ]

    links: list[SankeyLink] = []
    for i in range(len(paths)):
        stages = sorted(s for (pi, s) in rep if pi == i)
        for src_stage, dst_stage in zip(stages, stages[1:], strict=False):
            links.append(
                SankeyLink(
                    source=f"{src_stage}-{name_of[rep[(i, src_stage)]]}",
                    target=f"{dst_stage}-{name_of[rep[(i, dst_stage)]]}",
                    path_id=i,
                )
            )
        if stages:
            last = stages[-1]
            links.append(
                SankeyLink(
                    source=f"{last}-{name_of[rep[(i, last)]]}",
                    target=f"{OUTCOME_STAGE}-{alumni[i].career_area}",
                    path_id=i,
                )
            )

    return SankeyData(nodes=nodes, links=links)
