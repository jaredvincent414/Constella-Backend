"""Seed synthetic employment outcomes for the existing (placeholder) alumni.

MIDFIELD stops at the degree — no jobs — but the constellation clusters on
career outcome, so this fabricates a plausible outcome per alumnus from their
degree field. Everything written here is `provenance='synthetic'` and must be
labeled as such in the UI; it exists so the outcome-clustering path can be built
and demoed before real career-center data arrives.

Assignment is deterministic (hashed on the alumnus id), so the same corpus
always gets the same outcomes — scoring/cluster changes stay reviewable as
diffs. Idempotent: existing outcomes are cleared first.

    python -m scripts.seed_outcomes            # write outcomes
    python -m scripts.seed_outcomes --dry-run  # just show the industry mix
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from collections import Counter

from sqlalchemy import delete, select

from app.db import SessionLocal
from app.models import Alumnus, CareerOutcome, Provenance

# Academic CIP family -> plausible (industry, occupation) outcomes. Industries
# are drawn from a small shared set so the constellation clusters cleanly.
OUTCOMES: dict[str, list[tuple[str, str]]] = {
    "Business & Management": [
        ("Finance", "Financial Analyst"),
        ("Consulting", "Management Consultant"),
        ("Consumer Goods", "Brand Manager"),
        ("Technology", "Operations Manager"),
    ],
    "Engineering": [
        ("Technology", "Software Engineer"),
        ("Manufacturing", "Mechanical Engineer"),
        ("Aerospace & Defense", "Systems Engineer"),
        ("Energy", "Electrical Engineer"),
    ],
    "Computer & Information Sciences": [
        ("Technology", "Software Engineer"),
        ("Finance", "Data Engineer"),
        ("Technology", "Data Scientist"),
    ],
    "Biological & Biomedical Sciences": [
        ("Pharmaceuticals & Biotech", "Research Associate"),
        ("Healthcare", "Clinical Research Coordinator"),
        ("Pharmaceuticals & Biotech", "Process Scientist"),
    ],
    "Physical Sciences": [
        ("Energy", "Research Scientist"),
        ("Pharmaceuticals & Biotech", "Lab Scientist"),
        ("Technology", "Data Analyst"),
    ],
    "Mathematics & Statistics": [
        ("Finance", "Quantitative Analyst"),
        ("Technology", "Data Scientist"),
        ("Insurance", "Actuary"),
    ],
    "Psychology": [
        ("Healthcare", "Behavioral Therapist"),
        ("Education", "School Counselor"),
        ("Consulting", "UX Researcher"),
    ],
    "Social Sciences": [
        ("Government & Public Policy", "Policy Analyst"),
        ("Nonprofit", "Program Coordinator"),
        ("Marketing & Advertising", "Market Researcher"),
    ],
    "Communication & Journalism": [
        ("Media & Entertainment", "Reporter"),
        ("Marketing & Advertising", "Communications Specialist"),
        ("Media & Entertainment", "Content Producer"),
    ],
    "English Language & Literature": [
        ("Media & Entertainment", "Editor"),
        ("Marketing & Advertising", "Content Strategist"),
        ("Education", "Writing Instructor"),
    ],
    "Visual & Performing Arts": [
        ("Media & Entertainment", "Designer"),
        ("Marketing & Advertising", "Creative Director"),
        ("Technology", "UX Designer"),
    ],
    # The one key both corpora share — "Education" is a CIP family *and* a
    # DOMAINS label. Kept homogeneous so it behaves like the domain entries
    # below: a third of these alumni previously landed in Nonprofit, which split
    # the Education cluster in the synthetic corpus.
    "Education": [
        ("Education", "Teacher"),
        ("Education", "Curriculum Designer"),
        ("Education", "Program Manager"),
    ],
    "Natural Resources & Conservation": [
        ("Energy", "Environmental Analyst"),
        ("Government & Public Policy", "Conservation Scientist"),
        ("Nonprofit", "Sustainability Coordinator"),
    ],
    "Agriculture": [
        ("Agriculture & Food", "Agronomist"),
        ("Consumer Goods", "Food Scientist"),
        ("Government & Public Policy", "Agricultural Analyst"),
    ],
    "Family & Consumer Sciences": [
        ("Consumer Goods", "Product Specialist"),
        ("Healthcare", "Community Health Worker"),
        ("Education", "Extension Educator"),
    ],
    "Architecture": [
        ("Real Estate & Construction", "Architect"),
        ("Real Estate & Construction", "Project Manager"),
        ("Government & Public Policy", "Urban Planner"),
    ],
    "History": [
        ("Education", "Educator"),
        ("Government & Public Policy", "Research Analyst"),
        ("Nonprofit", "Archivist"),
    ],
    "Foreign Languages & Linguistics": [
        ("Education", "Language Instructor"),
        ("Technology", "Localization Specialist"),
        ("Government & Public Policy", "Foreign Service Analyst"),
    ],
    "Interdisciplinary Studies": [
        ("Consulting", "Analyst"),
        ("Nonprofit", "Program Coordinator"),
        ("Technology", "Product Analyst"),
    ],
    "Liberal Arts & Humanities": [
        ("Education", "Instructor"),
        ("Marketing & Advertising", "Content Strategist"),
        ("Nonprofit", "Program Coordinator"),
    ],
    "Area, Ethnic & Gender Studies": [
        ("Nonprofit", "Program Coordinator"),
        ("Government & Public Policy", "Policy Analyst"),
        ("Education", "Educator"),
    ],
    "Philosophy & Religious Studies": [
        ("Education", "Educator"),
        ("Nonprofit", "Program Coordinator"),
        ("Legal", "Paralegal"),
    ],
    "Engineering Technology": [
        ("Manufacturing", "Engineering Technician"),
        ("Energy", "Field Engineer"),
        ("Technology", "Systems Analyst"),
    ],
    "Health Professions": [
        ("Healthcare", "Clinician"),
        ("Healthcare", "Health Administrator"),
        ("Pharmaceuticals & Biotech", "Clinical Specialist"),
    ],
    "Public Administration & Social Service": [
        ("Government & Public Policy", "Public Administrator"),
        ("Nonprofit", "Social Worker"),
        ("Government & Public Policy", "Program Analyst"),
    ],
    # --- scripts/seed.py corpus -------------------------------------------
    # The synthetic corpus keys `career_area` on DOMAINS labels, not CIP
    # families, so without these every seeded alumnus falls through to
    # DEFAULT_OUTCOMES — three generic industries covering 93% of the corpus.
    # That matters because `outcome_industry` prefers a CareerOutcome's industry
    # over `career_area`, so seeding outcomes would *replace* the clustering axis
    # with a flatter one. Industry mirrors the label here to keep the cluster set
    # identical either way; what these rows actually add is the occupation,
    # region, years, and the `synthetic` provenance the UI has to surface.
    "Health Policy": [
        ("Health Policy", "Health Policy Analyst"),
        ("Health Policy", "Program Manager"),
        ("Health Policy", "Health Economist"),
    ],
    "Biotech": [
        ("Biotech", "Research Associate"),
        ("Biotech", "Process Scientist"),
        ("Biotech", "Bioinformatics Analyst"),
    ],
    "UX Research": [
        ("UX Research", "UX Researcher"),
        ("UX Research", "Design Researcher"),
        ("UX Research", "Product Designer"),
    ],
    "Software Engineering": [
        ("Software Engineering", "Software Engineer"),
        ("Software Engineering", "Backend Engineer"),
        ("Software Engineering", "Platform Engineer"),
    ],
    "Data Science": [
        ("Data Science", "Data Scientist"),
        ("Data Science", "Machine Learning Engineer"),
        ("Data Science", "Data Analyst"),
    ],
    "Finance": [
        ("Finance", "Financial Analyst"),
        ("Finance", "Investment Analyst"),
        ("Finance", "Quantitative Analyst"),
    ],
    "Environmental Policy": [
        ("Environmental Policy", "Environmental Analyst"),
        ("Environmental Policy", "Sustainability Coordinator"),
        ("Environmental Policy", "Conservation Policy Advisor"),
    ],
    "Product Management": [
        ("Product Management", "Product Manager"),
        ("Product Management", "Technical Program Manager"),
        ("Product Management", "Product Operations Lead"),
    ],
    "Journalism": [
        ("Journalism", "Reporter"),
        ("Journalism", "Editor"),
        ("Journalism", "Content Producer"),
    ],
}

DEFAULT_OUTCOMES = [
    ("Professional Services", "Analyst"),
    ("Technology", "Associate"),
    ("Nonprofit", "Coordinator"),
]

REGIONS = ["Northeast", "Southeast", "Midwest", "West", "Southwest", "Remote"]


def _hash(alumnus_id: str) -> int:
    return int(hashlib.sha256(alumnus_id.encode()).hexdigest(), 16)


def outcome_for(alumnus_id: str, career_area: str) -> tuple[str, str, str, int]:
    """Deterministic (industry, occupation, region, years_post_grad)."""
    options = OUTCOMES.get(career_area, DEFAULT_OUTCOMES)
    h = _hash(alumnus_id)
    industry, occupation = options[h % len(options)]
    region = REGIONS[(h // 7) % len(REGIONS)]
    years_post_grad = 1 + (h // 13) % 6
    return industry, occupation, region, years_post_grad


async def seed_outcomes(dry_run: bool = False) -> dict:
    async with SessionLocal() as session:
        rows = (await session.execute(select(Alumnus.id, Alumnus.career_area))).all()

        industries: Counter[str] = Counter()
        if not dry_run:
            await session.execute(delete(CareerOutcome))

        for alumnus_id, career_area in rows:
            industry, occupation, region, years = outcome_for(alumnus_id, career_area)
            industries[industry] += 1
            if not dry_run:
                session.add(
                    CareerOutcome(
                        alumnus_id=alumnus_id,
                        industry=industry,
                        occupation=occupation,
                        employer_region=region,
                        years_post_grad=years,
                        provenance=Provenance.synthetic,
                    )
                )
        if not dry_run:
            await session.commit()

    print(f"Alumni: {len(rows)} | outcomes {'previewed' if dry_run else 'written'} "
          f"across {len(industries)} industries")
    for industry, n in industries.most_common():
        print(f"  {n:5d}  {industry}")
    if dry_run:
        print("(dry run — nothing written)")
    return {"alumni": len(rows), "industries": dict(industries)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic employment outcomes")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = parser.parse_args()
    asyncio.run(seed_outcomes(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
