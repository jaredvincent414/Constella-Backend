"""The source adapter contract.

Implement this Protocol to teach Constella about a new dataset. The whole point
of the ingest layer is that this is the *only* interface a new data source has
to satisfy — produce `AlumnusRecord`s and `StudentRecord`s, and the loader,
matching engine, cache, and API all work unchanged.

    class MyRegistryAdapter:
        name = "my-university"

        def alumni(self) -> Iterator[AlumnusRecord]:
            ...

        def students(self) -> Iterator[StudentRecord]:
            ...

Register it in `app.ingest.sources` and select it with `--source my-university`
(or the `data_source` setting). See `app/ingest/sources/template.py` for a
worked skeleton and `app/ingest/README.md` for the migration checklist.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from app.ingest.records import AlumnusRecord, StudentRecord


@runtime_checkable
class SourceAdapter(Protocol):
    #: Stable identifier used on the CLI (`--source`) and in the registry.
    name: str

    def alumni(self) -> Iterator[AlumnusRecord]:
        """Yield every graduate to load, as source-neutral records."""
        ...

    def students(self) -> Iterator[StudentRecord]:
        """Yield the current-student profiles to load (may be empty)."""
        ...
