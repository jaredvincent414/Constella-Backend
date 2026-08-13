"""Source adapter registry.

To add a real-data source: implement `SourceAdapter` (see `template.py`), import
it here, and add one line to `_REGISTRY`. Selecting it is then just
`--source <name>` or the `data_source` setting — no other code changes.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import settings
from app.ingest.base import SourceAdapter
from app.ingest.sources.midfield import MidfieldAdapter

# name -> factory(path, **options) -> SourceAdapter
_REGISTRY: dict[str, Callable[..., SourceAdapter]] = {
    MidfieldAdapter.name: MidfieldAdapter,
    # "my-university": MyUniversityAdapter,   # <- real data drops in here
}


def available_sources() -> list[str]:
    return sorted(_REGISTRY)


def get_adapter(
    name: str | None = None,
    *,
    path: str | None = None,
    alumni_limit: int | None = None,
    student_count: int | None = None,
    seed: int | None = None,
) -> SourceAdapter:
    """Construct the configured source adapter.

    Unset arguments fall back to `app.config.settings`, so a bare
    `get_adapter()` builds whatever the environment is configured for.
    """
    name = name or settings.data_source
    if name not in _REGISTRY:
        raise KeyError(f"unknown data source {name!r}; available: {available_sources()}")
    return _REGISTRY[name](
        path=path or settings.data_path,
        alumni_limit=settings.ingest_alumni_limit if alumni_limit is None else alumni_limit,
        student_count=settings.ingest_student_count if student_count is None else student_count,
        seed=settings.ingest_seed if seed is None else seed,
    )
