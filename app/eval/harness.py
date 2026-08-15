"""Offline evaluation of the matching engine.

Three questions, none of which a unit test answers:

1. **Does each component do anything?** A weighted component that returns the
   same value for every alumnus consumes its weight and ranks nothing. Per-alumnus
   assertions can't see it; a corpus-wide distribution can.
2. **Is the ranking better than chance?** Held-out pivot prediction, below.
3. **Does a component change the ordering?** Ablation by rank correlation. A
   component can have a healthy spread and still not move the ranking.

Ground truth without labels
---------------------------
There is no labelled "these alumni were genuinely useful to this student" set,
and inventing one would be circular — the career outcomes are
`provenance='synthetic'`, so a hand-built golden set would grade the engine
against data we generated.

Instead the corpus grades itself. Take an alumnus who pivoted, rewind them to
the moment of the pivot — their pre-pivot transcript, their origin major, the
year it happened — and treat that as a student query. Where they *actually*
ended up is a real, reported-by-the-source label. The question becomes: does the
engine rank alumni who landed in that same place above the rest?

This is the same construction `midfield.students()` already uses to synthesize
demo students, reused as an evaluation set.

**The held-out profile carries no `intended_direction`.** Filling it with the
alumnus's real destination would hand the scorer the answer — `major_match`
scores the destination directly — and the numbers would look excellent while
measuring nothing. This is a broad-explore query on purpose.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.eval.metrics import (
    Description,
    base_rate,
    describe,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    spearman,
)
from app.matching.outcomes import outcome_industry
from app.matching.programs import origin_majors
from app.matching.scoring import (
    WEIGHT_COURSE_OVERLAP,
    WEIGHT_INTEREST,
    WEIGHT_MAJOR_MATCH,
    WEIGHT_PIVOT_YEAR,
    ScoredAlumnus,
    StudentProfile,
    score_corpus,
)
from app.matching.text import normalize_course_code
from app.models import Alumnus

DEFAULT_K = (5, 10, 25)

# The four weighted components, in the order the formula applies them.
COMPONENTS: dict[str, float] = {
    "course_overlap": WEIGHT_COURSE_OVERLAP,
    "pivot_year_alignment": WEIGHT_PIVOT_YEAR,
    "major_match": WEIGHT_MAJOR_MATCH,
    "interest_overlap": WEIGHT_INTEREST,
}


def held_out_profile(alumnus: Alumnus) -> StudentProfile | None:
    """Rewind an alumnus to their pivot and read them as a student query.

    Returns None for anyone who never pivoted — there is no "moment before the
    change" to stand them at, so they can't pose the question this measures.
    """
    pivot = alumnus.first_pivot
    if pivot is None:
        return None

    codes: list[str] = []
    names: dict[str, str] = {}
    for course in alumnus.pre_pivot_courses():
        if course.dropped:
            continue
        code = normalize_course_code(course.course_code)
        if code not in names:
            codes.append(code)
            names[code] = course.course_name

    majors = origin_majors(alumnus)
    return StudentProfile(
        id=f"holdout-{alumnus.id}",
        year_index=pivot.year_index,
        declared_major=next(iter(sorted(majors)), None),
        # No intended_direction: that field is the destination, which is the
        # label. See the module docstring.
        intended_direction=None,
        interests=[],
        course_codes=codes,
        course_names=names,
        current_majors=set(majors),
    )


@dataclass
class HoldoutResult:
    """One held-out query."""

    alumnus_id: str
    destination: str
    relevant: int
    corpus_size: int
    base_rate: float
    precision_at: dict[int, float]
    recall_at: dict[int, float]
    reciprocal_rank: float


@dataclass
class HoldoutReport:
    results: list[HoldoutResult] = field(default_factory=list)
    k_values: tuple[int, ...] = DEFAULT_K

    @property
    def queries(self) -> int:
        return len(self.results)

    def mean_precision_at(self, k: int) -> float:
        if not self.results:
            return 0.0
        return sum(r.precision_at[k] for r in self.results) / len(self.results)

    def mean_recall_at(self, k: int) -> float:
        if not self.results:
            return 0.0
        return sum(r.recall_at[k] for r in self.results) / len(self.results)

    @property
    def mean_reciprocal_rank(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.reciprocal_rank for r in self.results) / len(self.results)

    @property
    def mean_base_rate(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.base_rate for r in self.results) / len(self.results)

    def lift_at(self, k: int) -> float:
        """precision@k relative to chance. 1.0 means the ranking adds nothing."""
        rate = self.mean_base_rate
        if rate <= 0:
            return 0.0
        return self.mean_precision_at(k) / rate


def evaluate_holdout(
    corpus: list[Alumnus],
    *,
    sample_size: int = 25,
    k_values: tuple[int, ...] = DEFAULT_K,
    seed: int = 42,
) -> HoldoutReport:
    """Score each sampled pivoter against the rest of the corpus."""
    candidates = [a for a in corpus if a.first_pivot is not None]
    rng = random.Random(seed)
    sample = rng.sample(candidates, min(sample_size, len(candidates)))

    report = HoldoutReport(k_values=k_values)
    for held_out in sample:
        profile = held_out_profile(held_out)
        if profile is None:
            continue
        # The alumnus never competes against themselves — they'd rank first on a
        # perfect transcript match and inflate every metric here.
        others = [a for a in corpus if a.id != held_out.id]
        destination = outcome_industry(held_out)
        relevant = {a.id for a in others if outcome_industry(a) == destination}
        if not relevant:
            # Nobody else landed there; the query has no findable answer.
            continue

        ranked = [s.alumnus.id for s in score_corpus(profile, others)]
        report.results.append(
            HoldoutResult(
                alumnus_id=held_out.id,
                destination=destination,
                relevant=len(relevant),
                corpus_size=len(others),
                base_rate=base_rate(len(others), relevant),
                precision_at={k: precision_at_k(ranked, relevant, k) for k in k_values},
                recall_at={k: recall_at_k(ranked, relevant, k) for k in k_values},
                reciprocal_rank=reciprocal_rank(ranked, relevant),
            )
        )
    return report


@dataclass
class ComponentReport:
    """Per-component behaviour across the sampled queries."""

    pooled: dict[str, Description]
    constant_for: dict[str, int]
    queries: int

    @property
    def dead(self) -> list[str]:
        """Components that were constant for *every* query — pure weight burn."""
        return [name for name, n in self.constant_for.items() if n == self.queries and n > 0]


def _component_values(scored: list[ScoredAlumnus], name: str) -> list[float]:
    return [getattr(s, name) for s in scored]


def component_distribution(
    profiles: list[StudentProfile], corpus: list[Alumnus]
) -> ComponentReport:
    """Distribution of each component, pooled across queries."""
    pooled: dict[str, list[float]] = {name: [] for name in COMPONENTS}
    pooled["total"] = []
    constant_for = dict.fromkeys(COMPONENTS, 0)

    for profile in profiles:
        scored = score_corpus(profile, corpus)
        if not scored:
            continue
        for name in COMPONENTS:
            values = _component_values(scored, name)
            pooled[name].extend(values)
            if describe(values).is_constant:
                constant_for[name] += 1
        pooled["total"].extend(s.total for s in scored)

    return ComponentReport(
        pooled={name: describe(values) for name, values in pooled.items()},
        constant_for=constant_for,
        queries=len(profiles),
    )


def ablation(profiles: list[StudentProfile], corpus: list[Alumnus]) -> dict[str, float]:
    """Rank correlation between the full model and the model without each component.

    Recomputes the weighted sum from the component values `score_corpus` already
    returns, so nothing has to be monkeypatched and the real weights are used.

    A correlation of 1.0 means dropping the component reorders nothing — it is
    inert here whatever its spread. Lower means it is doing ranking work.
    """
    correlations: dict[str, list[float]] = {name: [] for name in COMPONENTS}

    for profile in profiles:
        scored = score_corpus(profile, corpus)
        if len(scored) < 2:
            continue
        full = [
            sum(weight * getattr(s, name) for name, weight in COMPONENTS.items())
            for s in scored
        ]
        for dropped in COMPONENTS:
            without = [
                sum(
                    weight * getattr(s, name)
                    for name, weight in COMPONENTS.items()
                    if name != dropped
                )
                for s in scored
            ]
            correlations[dropped].append(spearman(full, without))

    return {
        name: (sum(values) / len(values) if values else 1.0)
        for name, values in correlations.items()
    }
