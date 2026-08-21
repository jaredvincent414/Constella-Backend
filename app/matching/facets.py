"""Explore facets — narrowing the corpus before it is scored.

The Explore page sends interests, a career area, and/or a major. These are
**filters, not score components**: they decide who is eligible to appear, and
the weighted formula then ranks whoever survives. Keeping them out of the
formula is deliberate — a facet the student chose is a hard constraint, and
folding it into the score would let a strong course overlap outvote an explicit
"show me Health Policy". It also means adding them changes no ranking and needs
no eval run.

Matching is fuzzy for the same reason the scorer's is: these arrive as free text
from a dropdown built off the corpus, and "Public Health" must still match
"Public Health Policy". Reusing `best_text_similarity` keeps one definition of
"close enough" rather than inventing a second.
"""

from __future__ import annotations

from app.config import settings
from app.matching.corpus import AlumnusView, Corpus
from app.matching.text import best_text_similarity, tokenize


def _matches_career_area(view: AlumnusView, career_area: str) -> bool:
    """Against where they ended up only — the academic career area and the
    employment industry — never their majors.

    The scorer's `destinations` includes final majors, because "where do I want
    to end up" can legitimately be phrased as a major. A facet is a different
    question. Matching majors here made "Aerospace & Defense" return 65 alumni
    of whom only 50 worked in it: an Aerospace Engineering graduate now in
    Manufacturing shares a token with the industry name and is not in it.
    """
    return (
        best_text_similarity(career_area, view.outcome_labels) >= settings.facet_match_threshold
    )


def _matches_major(view: AlumnusView, major: str) -> bool:
    """Either end of the path counts. A student filtering on "Biology" wants
    alumni who *started* there as much as ones who graduated in it — the pivot
    away is the interesting part, and excluding it would hide exactly the
    trajectories the product is about."""
    candidates = (*view.origin_majors, *view.final_majors)
    return best_text_similarity(major, candidates) >= settings.facet_match_threshold


def _matches_interests(view: AlumnusView, interests: list[str]) -> bool:
    """Any shared token qualifies — this is an OR across the selected chips.

    Requiring all of them would return nothing on a corpus where interests are
    sparse, and the frontend renders these as multi-select chips, which reads as
    "any of these" to a user.
    """
    wanted: set[str] = set()
    for interest in interests:
        wanted |= tokenize(interest)
    if not wanted:
        return True
    return bool(wanted & view.interest_tokens)


def parse_interests(raw: str | None) -> list[str]:
    """`?interests=Biology,Psychology` -> ["Biology", "Psychology"]."""
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def filter_by_facets(
    corpus: Corpus,
    interests: list[str] | None = None,
    career_area: str | None = None,
    major: str | None = None,
) -> Corpus:
    """Narrow a corpus to the alumni a facet selection admits.

    Facets combine with AND — each one the student sets is a constraint they
    expect to hold. Within `interests` the match is OR, per `_matches_interests`.

    Returns the corpus unchanged when nothing is set, so the broad explore query
    pays nothing for the feature existing.
    """
    interests = interests or []
    if not interests and not career_area and not major:
        return corpus

    kept = tuple(
        view
        for view in corpus.views
        if (not career_area or _matches_career_area(view, career_area))
        and (not major or _matches_major(view, major))
        and (not interests or _matches_interests(view, interests))
    )
    return Corpus(views=kept, school_id=corpus.school_id)
