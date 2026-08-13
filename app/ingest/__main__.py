"""Ingestion CLI.

    # Load the configured source (defaults from app.config) into Postgres:
    python -m app.ingest --reset

    # Preview the MIDFIELD transform without touching the database:
    python -m app.ingest --source midfield --dry-run --alumni 50

    # Full corpus:
    python -m app.ingest --source midfield --alumni 0 --reset

After a load, rebuild the caches:  python -m app.jobs.recompute
"""

from __future__ import annotations

import argparse
import asyncio
import itertools

from app.config import settings
from app.db import SessionLocal
from app.ingest import available_sources, get_adapter, load


def _dry_run(adapter) -> None:
    alumni = list(adapter.alumni())
    students = list(adapter.students())
    total_courses = sum(len(a.courses) for a in alumni)
    total_pivots = sum(len(a.pivots) for a in alumni)
    with_pivot = sum(1 for a in alumni if a.pivots)

    print(f"source            : {adapter.name}")
    print(f"alumni            : {len(alumni)}")
    print(f"  with a pivot    : {with_pivot}")
    print(f"  total courses   : {total_courses}")
    print(f"  total pivots    : {total_pivots}")
    print(f"students (synth)  : {len(students)}")

    areas = itertools.groupby(sorted(a.career_area for a in alumni))
    counts = sorted(((k, len(list(g))) for k, g in areas), key=lambda kv: -kv[1])
    print("\ntop career areas (clustering axis):")
    for area, n in counts[:10]:
        print(f"  {n:5d}  {area}")

    sample = next((a for a in alumni if a.pivots and a.courses), alumni[0] if alumni else None)
    if sample is not None:
        print(f"\nsample alumnus {sample.id}:")
        print(f"  outcome     : {sample.outcome_title} @ {sample.outcome_org}")
        print(f"  career_area : {sample.career_area}  grad {sample.graduation_year}")
        print(f"  majors      : {[m.name for m in sample.majors]}")
        for p in sample.pivots:
            print(f"  pivot       : sem {p.semester_index}  {p.from_major} -> {p.to_major}")
        print(f"  courses     : {len(sample.courses)} "
              f"({sum(c.dropped for c in sample.courses)} dropped)")
    print("\n(dry run — nothing written)")


async def _load(adapter, reset: bool) -> None:
    async with SessionLocal() as session:
        stats = await load(adapter, session, reset=reset)
    print(
        f"Loaded from {adapter.name}: {stats.alumni} alumni, {stats.students} students, "
        f"{stats.courses} courses, {stats.pivots} pivots."
    )
    print("Next: python -m app.jobs.recompute   (rebuilds the cached constellations)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load a data source into Constella")
    parser.add_argument("--source", default=settings.data_source,
                        help=f"one of {available_sources()} (default: {settings.data_source})")
    parser.add_argument("--path", default=None, help="override the data directory")
    parser.add_argument("--alumni", type=int, default=None,
                        help="max alumni to load (0 = all)")
    parser.add_argument("--students", type=int, default=None,
                        help="synthetic current students to generate")
    parser.add_argument("--reset", action="store_true",
                        help="clear the existing corpus before loading")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and summarize records without writing to the DB")
    args = parser.parse_args()

    adapter = get_adapter(
        args.source,
        path=args.path,
        alumni_limit=args.alumni,
        student_count=args.students,
    )

    if args.dry_run:
        _dry_run(adapter)
    else:
        asyncio.run(_load(adapter, reset=args.reset))


if __name__ == "__main__":
    main()
