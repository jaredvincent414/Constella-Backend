"""Run the evaluation harness against a school's corpus.

    python -m app.eval --school institution-j
    python -m app.eval --school institution-j --students 50 --json

Read it before and after any change to the scoring formula. The numbers that
matter are `lift` (is the ranking better than chance) and the `!` flags (is a
component consuming its weight without ranking anything).
"""

from __future__ import annotations

import argparse
import asyncio
import json

from app import repository
from app.db import SessionLocal
from app.eval.harness import (
    DEFAULT_K,
    ablation,
    component_distribution,
    evaluate_holdout,
    held_out_profile,
)
from app.eval.report import format_ablation, format_components, format_holdout


async def run(school_id: str | None, students: int, seed: int, k_values: tuple[int, ...]) -> dict:
    async with SessionLocal() as session:
        corpus = await repository.list_alumni(session, school_id=school_id)

    if not corpus:
        raise SystemExit(
            f"no alumni for school {school_id!r} — check the slug with "
            "`GET /api/students/schools`, or omit --school to pool every tenant"
        )

    holdout = evaluate_holdout(corpus, sample_size=students, k_values=k_values, seed=seed)
    # Reuse the same sampled queries for the diagnostics, so the distribution and
    # ablation describe exactly the ranking the metrics above were computed from.
    profiles = [
        p
        for p in (held_out_profile(a) for a in corpus if a.first_pivot is not None)
        if p is not None
    ][:students]

    components = component_distribution(profiles, corpus)
    correlations = ablation(profiles, corpus)

    print(f"corpus: {len(corpus)} alumni" + (f" · school {school_id}" if school_id else ""))
    print()
    print(format_components(components))
    print()
    print(format_ablation(correlations))
    print()
    print(format_holdout(holdout))

    return {
        "school_id": school_id,
        "corpus_size": len(corpus),
        "queries": holdout.queries,
        "base_rate": holdout.mean_base_rate,
        "precision_at": {k: holdout.mean_precision_at(k) for k in k_values},
        "recall_at": {k: holdout.mean_recall_at(k) for k in k_values},
        "lift_at": {k: holdout.lift_at(k) for k in k_values},
        "mrr": holdout.mean_reciprocal_rank,
        "dead_components": components.dead,
        "ablation": correlations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the matching engine offline")
    parser.add_argument(
        "--school",
        default=None,
        help="School slug to evaluate. Omit to pool every tenant — useful for a "
        "corpus-wide view, but not what any student actually sees.",
    )
    parser.add_argument("--students", type=int, default=25, help="Held-out queries to sample")
    parser.add_argument("--seed", type=int, default=42, help="Sampling seed (reproducibility)")
    parser.add_argument("--json", action="store_true", help="Also emit the summary as JSON")
    args = parser.parse_args()

    summary = asyncio.run(run(args.school, args.students, args.seed, DEFAULT_K))
    if args.json:
        print()
        print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
