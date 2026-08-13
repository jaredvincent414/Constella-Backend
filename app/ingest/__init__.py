"""Data ingestion — a swappable boundary between a dataset and the corpus.

    source adapter (dataset-specific)  ->  source-neutral records  ->  loader  ->  Postgres

The matching engine, cache, and API only ever see the ORM tables the loader
fills, so replacing the dataset means writing one adapter and flipping a config
value. See README.md in this package and `sources/template.py`.
"""

from __future__ import annotations

from app.ingest.loader import LoadStats, load, reset_corpus
from app.ingest.records import (
    AlumnusRecord,
    CourseRecord,
    MajorRecord,
    MilestoneRecord,
    PivotRecord,
    StudentRecord,
)
from app.ingest.sources import available_sources, get_adapter

__all__ = [
    "AlumnusRecord",
    "CourseRecord",
    "LoadStats",
    "MajorRecord",
    "MilestoneRecord",
    "PivotRecord",
    "StudentRecord",
    "available_sources",
    "get_adapter",
    "load",
    "reset_corpus",
]
