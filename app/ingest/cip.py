"""CIP code helpers for the MIDFIELD adapter.

MIDFIELD encodes majors as 6-digit CIP codes (`140901` = Mechanical
Engineering). Two derivations are needed:

* a human-readable **major name** for the scorer's text-similarity and for pivot
  from/to labels — built at load time from the degree table, which pairs each
  CIP with a readable degree string;
* a coarse **career area** for clustering. MIDFIELD has no employment data, so
  we cluster on the 2-digit CIP family (the academic discipline) instead. This
  is the honest stand-in until real career outcomes arrive: see the note in
  `sources/midfield.py`.

The 2-digit family list is the stable NCES CIP series; it does not change with
the sample, so it lives here as a literal rather than being derived from data.
"""

from __future__ import annotations

import re

# NCES 2-digit CIP families -> a readable discipline label used as the cluster
# ("career area") name. Phrased as an area a student would recognize.
CIP_FAMILIES: dict[str, str] = {
    "01": "Agriculture",
    "03": "Natural Resources & Conservation",
    "04": "Architecture",
    "05": "Area, Ethnic & Gender Studies",
    "09": "Communication & Journalism",
    "10": "Communications Technology",
    "11": "Computer & Information Sciences",
    "12": "Personal & Culinary Services",
    "13": "Education",
    "14": "Engineering",
    "15": "Engineering Technology",
    "16": "Foreign Languages & Linguistics",
    "19": "Family & Consumer Sciences",
    "22": "Legal Professions",
    "23": "English Language & Literature",
    "24": "Liberal Arts & Humanities",
    "25": "Library Science",
    "26": "Biological & Biomedical Sciences",
    "27": "Mathematics & Statistics",
    "28": "Military Science",
    "29": "Military Technologies",
    "30": "Interdisciplinary Studies",
    "31": "Parks, Recreation & Fitness",
    "38": "Philosophy & Religious Studies",
    "39": "Theology & Religious Vocations",
    "40": "Physical Sciences",
    "41": "Science Technologies",
    "42": "Psychology",
    "43": "Homeland Security & Law Enforcement",
    "44": "Public Administration & Social Service",
    "45": "Social Sciences",
    "46": "Construction Trades",
    "47": "Mechanic & Repair Technologies",
    "48": "Precision Production",
    "49": "Transportation",
    "50": "Visual & Performing Arts",
    "51": "Health Professions",
    "52": "Business & Management",
    "53": "Secondary Education Credentials",
    "54": "History",
}

# Strips the credential prefix from a degree string so it reads as a major name:
#   "Bachelor of Science in Electrical Engineering" -> "Electrical Engineering"
#   "Bachelor Arts in Environmental Studies"        -> "Environmental Studies"
_DEGREE_PREFIX = re.compile(
    r"^(bachelor|master|associate|doctor)?\s*(of|in)?\s*"
    r"(science|arts|philosophy|engineering|fine arts|business administration)?\s*(in|of)?\s+",
    re.IGNORECASE,
)


def career_area_for_cip(cip6: str | None) -> str:
    """Map a 6-digit CIP to its discipline label. Unknown families -> 'Other'."""
    if not cip6:
        return "Other"
    return CIP_FAMILIES.get(cip6[:2].zfill(2), "Other")


def clean_degree_name(degree: str) -> str:
    """Turn a degree string into a bare major name.

    Falls back to the original text if the prefix pattern doesn't match, so an
    unusual credential label is never silently blanked.
    """
    cleaned = _DEGREE_PREFIX.sub("", degree.strip(), count=1).strip()
    return cleaned or degree.strip()
