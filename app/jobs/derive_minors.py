"""Derive de facto minors from course concentration (placeholder data only).

The MIDFIELD sample carries no minors, but the product story leans on
non-traditional combinations. Where a student piled up coursework in a field
*outside* their declared major, we infer a minor — tagged `provenance='derived'`
so nothing here is ever presented as a formally declared program.

This is a **separate, offline job**, never part of ingest: the raw import stays a
faithful copy of the source, and the inference is a step you can rerun, retune,
or throw away without reloading.

    python -m app.jobs.derive_minors            # write derived minors
    python -m app.jobs.derive_minors --dry-run  # just report the distribution

Rerunning is idempotent: existing derived rows are cleared first.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict

import structlog
from sqlalchemy import delete

from app.config import settings
from app.db import SessionLocal
from app.ingest.cip import career_area_for_cip
from app.matching.programs import all_minors
from app.matching.text import text_similarity
from app.models import MAJOR_ROLES, AlumnusMajor, Provenance
from app.repository import list_alumni

log = structlog.get_logger(__name__)

# A concentration counts as "outside the field" when its discipline family is
# this dissimilar from every declared major's family. Not a course/credit
# threshold (those are configurable) — a name-matching cutoff.
_OUTSIDE_FAMILY_SIM = 0.3


def _discipline_family(discipline: str) -> str:
    """'Engineering: Electrical and Computer' -> 'Engineering'."""
    return discipline.split(":")[0].strip()


def _home_families(alumnus) -> set[str]:
    """CIP families of the alumnus's declared majors — their home turf."""
    families: set[str] = set()
    for program in alumnus.majors:
        role = program.role or None
        if role is not None and role not in MAJOR_ROLES:
            continue
        if program.cip6:
            families.add(career_area_for_cip(program.cip6))
    return families


def _is_outside(family: str, home: set[str]) -> bool:
    return all(text_similarity(family, h) < _OUTSIDE_FAMILY_SIM for h in home)


def derive_for_alumnus(alumnus, min_courses: int, min_credits: float) -> list[tuple[str, int]]:
    """Return (minor_name, last_term) pairs inferred from course concentration."""
    home = _home_families(alumnus)
    declared_minor_names = all_minors(alumnus)

    counts: Counter[str] = Counter()
    credits: dict[str, float] = defaultdict(float)
    last_term: dict[str, int] = {}
    for course in alumnus.courses:
        if course.dropped or not course.discipline:
            continue
        family = _discipline_family(course.discipline)
        if not family:
            continue
        counts[family] += 1
        credits[family] += course.credit_hours or 0.0
        last_term[family] = max(last_term.get(family, 0), course.semester_index)

    derived: list[tuple[str, int]] = []
    for family, n in counts.items():
        if family in home or family in declared_minor_names:
            continue
        if n < min_courses and credits[family] < min_credits:
            continue
        if not _is_outside(family, home):
            continue
        derived.append((family, last_term[family]))
    return derived


async def derive_minors(dry_run: bool = False) -> dict:
    min_courses = settings.derived_minor_min_courses
    min_credits = settings.derived_minor_min_credits

    async with SessionLocal() as session:
        alumni = await list_alumni(session)

        per_student: Counter[int] = Counter()
        total = 0
        for alumnus in alumni:
            derived = derive_for_alumnus(alumnus, min_courses, min_credits)
            per_student[len(derived)] += 1
            total += len(derived)

            if dry_run:
                continue

            # Idempotent: clear this alumnus's previously derived rows first.
            await session.execute(
                delete(AlumnusMajor).where(
                    AlumnusMajor.alumnus_id == alumnus.id,
                    AlumnusMajor.provenance == Provenance.derived,
                )
            )
            for name, term in derived:
                session.add(
                    AlumnusMajor(
                        alumnus_id=alumnus.id,
                        name=name,
                        declared_semester=term,
                        is_final=True,
                        role="minor",
                        provenance=Provenance.derived,
                    )
                )
        if not dry_run:
            await session.commit()

    distribution = {k: per_student[k] for k in sorted(per_student)}
    log.info(
        "derived_minors",
        alumni=len(alumni),
        total_minors=total,
        min_courses=min_courses,
        min_credits=min_credits,
        distribution=distribution,
        dry_run=dry_run,
    )
    print(f"Alumni: {len(alumni)} | derived minors: {total}"
          f"{' (dry run — nothing written)' if dry_run else ''}")
    print("Derived minors per student (count: how many students):")
    for k in sorted(per_student):
        print(f"  {k}: {per_student[k]}")
    return {"alumni": len(alumni), "total_minors": total, "distribution": distribution}


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive de facto minors from coursework")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = parser.parse_args()
    asyncio.run(derive_minors(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
