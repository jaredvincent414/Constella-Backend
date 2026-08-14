"""Seed the database with synthetic alumni and sample students.

Run with a fixed RNG seed so the corpus is reproducible: the same seed always
produces the same alumni, which makes scoring changes reviewable as diffs
rather than as noise.

    python -m scripts.seed --alumni 240 --reset
"""

from __future__ import annotations

import argparse
import asyncio
import random

from sqlalchemy import delete

from app.auth import hash_token
from app.db import SessionLocal
from app.models import (
    Alumnus,
    AlumnusCourse,
    AlumnusMajor,
    Milestone,
    Pivot,
    School,
    Student,
    StudentCourse,
    StudentYear,
)

# Two schools, so the tenant boundary is something you can actually exercise
# locally: a token from one school must 404 on the other's alumni ids.
SCHOOLS = [("demo-university", "Demo University"), ("second-college", "Second College")]

# Fixed tokens for the demo students. Predictable *on purpose* — you cannot curl
# an authenticated endpoint with a token you can't see, and registration only
# ever shows a token once. Nothing outside this seed script mints tokens this
# way, and seeding a production database is not a supported operation.
DEV_TOKENS = {
    "student-demo": "dev-token-student-demo",
    "student-undeclared": "dev-token-student-undeclared",
    "student-junior-cs": "dev-token-student-junior-cs",
    "student-other-school": "dev-token-student-other-school",
}

# Courses nearly everyone takes in their first year. These are what make course
# overlap meaningful across domains — without a shared foundation, a student's
# transcript would only ever intersect with their own field.
FOUNDATION = [
    ("WRIT 101", "Writing Seminar"),
    ("MATH 101", "Calculus I"),
    ("BIO 101", "Bio 101"),
    ("CHEM 101", "Chem 101"),
    ("PSYC 101", "Intro Psych"),
    ("SOC 101", "Sociology 101"),
    ("ECON 101", "Microeconomics"),
    ("CS 101", "Intro to Computer Science"),
]

# Each domain is a coherent academic track: the majors that lead into it, the
# courses that signal it, and where its graduates end up.
DOMAINS: dict[str, dict] = {
    "Health Policy": {
        "majors": ["Public Health", "Health Policy", "Biochemistry"],
        "courses": [
            ("PH 201", "Intro to Public Health"),
            ("PH 310", "Epidemiology"),
            ("PH 320", "Health Policy"),
            ("STAT 240", "Biostatistics"),
            ("PH 350", "Community Health"),
            ("PH 410", "Global Health Systems"),
        ],
        "outcomes": [
            ("Health Policy Analyst", "State Health Dept"),
            ("Program Manager", "County Public Health"),
            ("Policy Researcher", "Health Equity Institute"),
        ],
        "interests": ["Global Health Club", "Public Health Internship", "Health Equity Research"],
    },
    "Biotech": {
        "majors": ["Biochemistry", "Molecular Biology", "Chemistry"],
        "courses": [
            ("CHEM 210", "Organic Chem"),
            ("BIO 230", "Genetics"),
            ("BIO 240", "Cell Biology"),
            ("CHEM 315", "Biochem Lab"),
            ("BIO 380", "Immunology"),
            ("BIO 410", "Protein Engineering"),
        ],
        "outcomes": [
            ("Research Associate", "Genomics Startup"),
            ("Process Scientist", "Biologics Manufacturing"),
            ("Lab Manager", "University Research Lab"),
        ],
        "interests": ["Undergraduate Research", "Biotech Club", "Lab Internship"],
    },
    "UX Research": {
        "majors": ["Cognitive Science", "Psychology", "Design"],
        "courses": [
            ("COGS 210", "Human-Computer Interaction"),
            ("PSYC 250", "Research Methods"),
            ("PSYC 330", "Cognitive Psychology"),
            ("DES 200", "Design Studio"),
            ("COGS 340", "Usability Testing"),
            ("DES 350", "Interaction Design"),
        ],
        "outcomes": [
            ("UX Researcher", "Consumer Software Co"),
            ("Design Researcher", "Product Studio"),
            ("Research Lead", "Fintech Platform"),
        ],
        "interests": ["Design Club", "UX Internship", "Accessibility Advocacy"],
    },
    "Software Engineering": {
        "majors": ["Computer Science", "Mathematics"],
        "courses": [
            ("CS 201", "Data Structures"),
            ("CS 310", "Algorithms"),
            ("CS 330", "Operating Systems"),
            ("CS 340", "Databases"),
            ("CS 450", "Distributed Systems"),
            ("CS 370", "Software Architecture"),
        ],
        "outcomes": [
            ("Software Engineer", "Infrastructure Company"),
            ("Backend Engineer", "Payments Platform"),
            ("Platform Engineer", "Developer Tools Co"),
        ],
        "interests": ["Hackathon Club", "Open Source Contributor", "SWE Internship"],
    },
    "Data Science": {
        "majors": ["Statistics", "Computer Science", "Mathematics"],
        "courses": [
            ("STAT 210", "Probability"),
            ("STAT 320", "Statistical Inference"),
            ("CS 380", "Machine Learning"),
            ("MATH 220", "Linear Algebra"),
            ("STAT 410", "Data Mining"),
            ("CS 385", "Applied ML"),
        ],
        "outcomes": [
            ("Data Scientist", "Analytics Firm"),
            ("ML Engineer", "Recommendation Team"),
            ("Quantitative Analyst", "Research Group"),
        ],
        "interests": ["Data Science Club", "Kaggle Competitions", "Research Assistantship"],
    },
    "Finance": {
        "majors": ["Economics", "Mathematics", "Business"],
        "courses": [
            ("ECON 202", "Macroeconomics"),
            ("FIN 310", "Corporate Finance"),
            ("ECON 340", "Econometrics"),
            ("FIN 350", "Financial Modeling"),
            ("FIN 420", "Investment Analysis"),
            ("ECON 380", "Behavioral Economics"),
        ],
        "outcomes": [
            ("Financial Analyst", "Investment Bank"),
            ("Strategy Associate", "Consulting Firm"),
            ("Risk Analyst", "Asset Manager"),
        ],
        "interests": ["Investment Club", "Finance Internship", "Case Competition Team"],
    },
    "Environmental Policy": {
        "majors": ["Environmental Science", "Political Science", "Biology"],
        "courses": [
            ("ENV 210", "Ecology"),
            ("ENV 320", "Environmental Policy"),
            ("ENV 330", "Climate Science"),
            ("POLI 250", "Public Policy"),
            ("ENV 410", "Conservation Biology"),
            ("ENV 360", "Energy Systems"),
        ],
        "outcomes": [
            ("Policy Analyst", "Environmental Agency"),
            ("Sustainability Consultant", "Climate Advisory"),
            ("Program Officer", "Conservation Nonprofit"),
        ],
        "interests": ["Sustainability Coalition", "Field Research", "Policy Internship"],
    },
    "Education": {
        "majors": ["Education", "Sociology", "English"],
        "courses": [
            ("EDU 210", "Learning Sciences"),
            ("EDU 300", "Educational Psychology"),
            ("EDU 340", "Curriculum Design"),
            ("SOC 260", "Sociology of Education"),
            ("EDU 420", "Assessment & Equity"),
            ("EDU 380", "Classroom Practice"),
        ],
        "outcomes": [
            ("Curriculum Designer", "Education Nonprofit"),
            ("Program Coordinator", "School District"),
            ("Learning Designer", "EdTech Company"),
        ],
        "interests": ["Tutoring Program", "Education Policy Club", "Teaching Fellowship"],
    },
    "Product Management": {
        "majors": ["Business", "Computer Science", "Cognitive Science"],
        "courses": [
            ("BUS 210", "Marketing"),
            ("BUS 320", "Operations"),
            ("BUS 400", "Strategy"),
            ("DES 240", "Product Design"),
            ("BUS 350", "Analytics for Managers"),
            ("CS 260", "Systems for Product"),
        ],
        "outcomes": [
            ("Associate Product Manager", "SaaS Company"),
            ("Product Manager", "Marketplace Platform"),
            ("Product Analyst", "Consumer App"),
        ],
        "interests": ["Product Club", "Startup Incubator", "PM Internship"],
    },
    "Journalism": {
        "majors": ["English", "Communications", "Political Science"],
        "courses": [
            ("ENG 220", "Narrative Nonfiction"),
            ("COMM 300", "Journalism Practice"),
            ("COMM 340", "Media Studies"),
            ("ENG 350", "Rhetoric"),
            ("COMM 410", "Investigative Reporting"),
            ("POLI 230", "Political Communication"),
        ],
        "outcomes": [
            ("Reporter", "Regional Newsroom"),
            ("Content Strategist", "Media Company"),
            ("Communications Associate", "Advocacy Group"),
        ],
        "interests": ["Student Newspaper", "Radio Station", "Newsroom Internship"],
    },
}

DOMAIN_NAMES = list(DOMAINS)


def pick_courses(
    rng: random.Random, domain: str, count: int, intro_only: bool = False
) -> list[tuple[str, str]]:
    """Sample courses from a domain.

    `intro_only` restricts to the first two entries, which are the lowest-level
    courses in each pool — a freshman shouldn't be enrolled in Protein
    Engineering, and letting that happen would inflate course overlap for
    students who haven't taken advanced courses yet.
    """
    pool = DOMAINS[domain]["courses"]
    if intro_only:
        pool = pool[:2]
    return rng.sample(pool, min(count, len(pool)))


def generate_alumnus(rng: random.Random, index: int, school_id: str) -> Alumnus:
    origin_domain = rng.choice(DOMAIN_NAMES)

    # ~70% pivot. The rest graduate in the direction they started, which the
    # scorer needs to handle without a pivot year to align against.
    pivots_at: int | None = None
    destination_domain = origin_domain
    if rng.random() < 0.7:
        others = [d for d in DOMAIN_NAMES if d != origin_domain]
        destination_domain = rng.choice(others)
        pivots_at = rng.choice([2, 3, 3, 4, 4, 5])

    origin_major = rng.choice(DOMAINS[origin_domain]["majors"])
    # Domains share majors (Biochemistry appears under both Biotech and Health
    # Policy), so a naive pick can produce a pivot from a major to itself.
    # Exclude the origin when there's an alternative.
    destination_majors = DOMAINS[destination_domain]["majors"]
    if pivots_at is not None:
        alternatives = [m for m in destination_majors if m != origin_major]
        destination_majors = alternatives or destination_majors
    final_major = rng.choice(destination_majors)
    graduation_year = rng.randint(2016, 2024)
    title, org = rng.choice(DOMAINS[destination_domain]["outcomes"])

    alumnus = Alumnus(
        id=f"alum-{index:04d}",
        school_id=school_id,
        graduation_year=graduation_year,
        outcome_title=title,
        outcome_org=org,
        career_area=destination_domain,
        interests=rng.sample(DOMAINS[destination_domain]["interests"], k=2),
    )

    # --- transcript -------------------------------------------------------
    boundary = pivots_at if pivots_at is not None else 8
    used_codes: set[str] = set()

    def add(code: str, name: str, semester: int, dropped: bool = False) -> None:
        if code in used_codes:
            return
        used_codes.add(code)
        alumnus.courses.append(
            AlumnusCourse(
                course_code=code,
                course_name=name,
                semester_index=semester,
                dropped=dropped,
            )
        )

    # Freshman year: mostly foundation, plus an early taste of the origin domain.
    for semester in (0, 1):
        for code, name in rng.sample(FOUNDATION, 3):
            add(code, name, semester)
        for code, name in pick_courses(rng, origin_domain, 1, intro_only=True):
            add(code, name, semester)

    for semester in range(2, 8):
        domain = origin_domain if semester < boundary else destination_domain
        for code, name in pick_courses(rng, domain, rng.randint(2, 3)):
            add(code, name, semester)

    # --- pivot ------------------------------------------------------------
    if pivots_at is not None:
        alumnus.pivots.append(
            Pivot(
                semester_index=pivots_at,
                from_major=origin_major,
                to_major=final_major,
                note=f"Shifted from {origin_domain} toward {destination_domain}",
            )
        )
        # A course abandoned at the pivot — this is what renders as [dropped].
        abandoned = pick_courses(rng, origin_domain, 1)
        if abandoned:
            code, name = abandoned[0]
            add(f"{code}-D", name, pivots_at, dropped=True)

    # --- majors -----------------------------------------------------------
    alumnus.majors.append(
        AlumnusMajor(name=origin_major, declared_semester=1, is_final=pivots_at is None)
    )
    if pivots_at is not None:
        alumnus.majors.append(
            AlumnusMajor(name=final_major, declared_semester=pivots_at, is_final=True)
        )
        # ~25% keep the original as a double major rather than dropping it.
        if rng.random() < 0.25:
            alumnus.majors.append(
                AlumnusMajor(name=origin_major, declared_semester=1, is_final=True)
            )

    # --- milestones -------------------------------------------------------
    alumnus.milestones.append(Milestone(semester_index=5, text=f"Internship: {org}"))
    alumnus.milestones.append(
        Milestone(semester_index=6, text=f"Senior Thesis: {destination_domain} Research")
    )
    alumnus.milestones.append(Milestone(semester_index=7, text="Graduated"))

    return alumnus


def sample_students() -> list[Student]:
    """A handful of students spanning the cases the frontend has to render.

    All but the last belong to the first school; `student-other-school` is the
    control for tenant isolation — its token must not reach any `alum-*` row the
    others can see.
    """
    students: list[Student] = []
    primary, secondary = SCHOOLS[0][0], SCHOOLS[1][0]

    # The spec's worked example: a sophomore on a science track considering
    # a move toward health policy.
    sophomore = Student(
        id="student-demo",
        school_id=primary,
        year=StudentYear.sophomore,
        declared_major="Biochemistry",
        intended_direction="Health Policy",
        interests=["Global Health Club", "Undergraduate Research"],
    )
    for semester, courses in {
        0: [("BIO 101", "Bio 101"), ("CHEM 101", "Chem 101"), ("PSYC 101", "Intro Psych")],
        1: [("CHEM 210", "Organic Chem"), ("MATH 101", "Calculus I"), ("SOC 101", "Sociology 101")],
        2: [("BIO 230", "Genetics"), ("PH 201", "Intro to Public Health")],
    }.items():
        for code, name in courses:
            sophomore.courses.append(
                StudentCourse(course_code=code, course_name=name, semester_index=semester)
            )
    students.append(sophomore)

    # An undeclared freshman — exercises the neutral major-match path and the
    # thin-transcript case the frontend flags as an open question.
    freshman = Student(
        id="student-undeclared",
        school_id=primary,
        year=StudentYear.freshman,
        declared_major=None,
        intended_direction=None,
        interests=["Hackathon Club"],
    )
    for code, name in [("CS 101", "Intro to Computer Science"), ("MATH 101", "Calculus I")]:
        freshman.courses.append(
            StudentCourse(course_code=code, course_name=name, semester_index=0)
        )
    students.append(freshman)

    # A junior deep into a CS track, considering product.
    junior = Student(
        id="student-junior-cs",
        school_id=primary,
        year=StudentYear.junior,
        declared_major="Computer Science",
        intended_direction="Product Management",
        interests=["Startup Incubator", "Open Source Contributor"],
    )
    for semester, courses in {
        0: [("CS 101", "Intro to Computer Science"), ("MATH 101", "Calculus I")],
        1: [("CS 201", "Data Structures"), ("WRIT 101", "Writing Seminar")],
        2: [("CS 310", "Algorithms"), ("MATH 220", "Linear Algebra")],
        3: [("CS 340", "Databases"), ("ECON 101", "Microeconomics")],
        4: [("CS 330", "Operating Systems"), ("BUS 210", "Marketing")],
    }.items():
        for code, name in courses:
            junior.courses.append(
                StudentCourse(course_code=code, course_name=name, semester_index=semester)
            )
    students.append(junior)

    # The isolation control: same shape as the demo sophomore, other tenant.
    other = Student(
        id="student-other-school",
        school_id=secondary,
        year=StudentYear.sophomore,
        declared_major="Biochemistry",
        intended_direction="Health Policy",
        interests=["Global Health Club"],
    )
    for code, name in [("BIO 101", "Bio 101"), ("CHEM 101", "Chem 101")]:
        other.courses.append(
            StudentCourse(course_code=code, course_name=name, semester_index=0)
        )
    students.append(other)

    for student in students:
        student.email = f"{student.id}@example.edu"
        student.auth_token_hash = hash_token(DEV_TOKENS[student.id])

    return students


async def seed(alumni_count: int, seed_value: int, reset: bool) -> None:
    rng = random.Random(seed_value)

    async with SessionLocal() as session:
        if reset:
            await session.execute(delete(Student))
            await session.execute(delete(Alumnus))
            await session.commit()

        # Schools are upserted, never deleted on --reset: students created
        # through the API reference them, and dropping one cascades those away.
        for school_id, school_name in SCHOOLS:
            if await session.get(School, school_id) is None:
                session.add(School(id=school_id, name=school_name))
        await session.commit()

        # Most of the corpus in the primary school, a slice in the second, so
        # both a populated constellation and a cross-tenant 404 are reachable.
        for index in range(alumni_count):
            school_id = SCHOOLS[0][0] if index % 5 else SCHOOLS[1][0]
            session.add(generate_alumnus(rng, index, school_id))
        for student in sample_students():
            session.add(student)

        await session.commit()

    students = sample_students()
    print(
        f"Seeded {alumni_count} alumni across {len(SCHOOLS)} schools "
        f"and {len(students)} students."
    )
    print("Dev bearer tokens (local only):")
    for student in students:
        print(f"  {student.id:<24} {student.school_id:<16} {DEV_TOKENS[student.id]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the Constella database")
    parser.add_argument("--alumni", type=int, default=240)
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for reproducibility")
    parser.add_argument("--reset", action="store_true", help="Delete existing rows first")
    args = parser.parse_args()

    asyncio.run(seed(args.alumni, args.seed, args.reset))


if __name__ == "__main__":
    main()
