"""Career-outcome accessors — the axis the constellation clusters on.

One place decides "where did this alumnus end up", so clustering, the combine
endpoint, the scoring destination, and the node/detail display all agree. When
an alumnus has a `CareerOutcome`, the industry is the cluster key and the
occupation/industry are what the node shows; otherwise everything falls back to
the academic `career_area` stand-in, so records without employment data (and the
whole existing test corpus) behave exactly as before.
"""

from __future__ import annotations

from app.models import Alumnus
from app.schemas.constellation import OutcomeOut


def outcome_industry(alumnus: Alumnus) -> str:
    """The clustering key: the alumnus's industry, else the academic fallback."""
    outcome = alumnus.primary_outcome
    return outcome.industry if outcome else alumnus.career_area


def build_outcome(alumnus: Alumnus) -> OutcomeOut:
    """The `OutcomeOut` for a node / detail panel.

    With employment data the title is the occupation and the org is the industry,
    carrying `provenance` so the UI can flag synthetic placeholders. Without it we
    fall back to the stored (academic) title/org.
    """
    outcome = alumnus.primary_outcome
    if outcome is None:
        return OutcomeOut(title=alumnus.outcome_title, org=alumnus.outcome_org)
    return OutcomeOut(
        title=outcome.occupation,
        org=outcome.industry,
        industry=outcome.industry,
        occupation=outcome.occupation,
        region=outcome.employer_region,
        provenance=outcome.provenance.value,
    )
