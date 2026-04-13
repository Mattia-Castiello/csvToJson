from __future__ import annotations

"""Entry-point del layer Resolver.

Instrada automaticamente verso resolver singolo o multi-segmento.
"""

from pathlib import Path

from quoro.models import Segment, TypedDocument
from quoro.resolver import multi_sheet_resolver, semantic_resolver


def resolve(
    segments: list[Segment],
    schema_dir: Path | None = None,
    model: str | None = None,
    confidence_threshold: float | None = None,
) -> list[TypedDocument]:
    """Risoluzione semantica dei segmenti verso documenti tipizzati.

    Se il file produce piu segmenti usa il resolver multi-sheet, altrimenti
    usa il resolver semantico singolo.
    """

    kwargs: dict = {}
    if schema_dir is not None:
        kwargs["schema_dir"] = schema_dir
    if model is not None:
        kwargs["model"] = model
    if confidence_threshold is not None:
        kwargs["confidence_threshold"] = confidence_threshold

    if len(segments) > 1:
        return multi_sheet_resolver.resolve_multi_sheet(segments, **kwargs)
    return semantic_resolver.resolve(segments, **kwargs)


__all__ = ["resolve"]
