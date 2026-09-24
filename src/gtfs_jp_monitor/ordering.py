"""Generation ordering (data-model §2).

Oldest first by from_date, then published_at, then uid. published_at is only a tie-breaker:
bulk re-uploads give many old generations the same published_at, so it cannot lead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Sequence

from .ids import rid_rank

_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class GenerationRef:
    """The fields ordering needs; anything with these attributes can be ordered."""

    uid: str
    from_date: str | None
    published_at: str | None
    rid: str | None = None


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_published_at(value: str | None) -> datetime | None:
    """Parse the API timestamp (with or without microseconds). Naive values are rejected."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def sort_key(gen: GenerationRef) -> tuple:
    # Callers must separate generations without a usable from_date first (see order_generations).
    from_date = parse_iso_date(gen.from_date)
    if from_date is None:
        raise ValueError(f"generation {gen.uid} has no usable from_date")
    published = parse_published_at(gen.published_at)
    # A missing or unparsable published_at sorts before any real timestamp on the same day.
    return (from_date, published is not None, published or _MIN_UTC, gen.uid)


def order_generations(gens: Iterable[GenerationRef]) -> tuple[list[GenerationRef], list[GenerationRef]]:
    """Return (ordered oldest-first, unorderable). Unorderable ones lack a usable from_date."""
    orderable: list[GenerationRef] = []
    unknown: list[GenerationRef] = []
    for gen in gens:
        (orderable if parse_iso_date(gen.from_date) else unknown).append(gen)
    orderable.sort(key=sort_key)
    unknown.sort(key=lambda g: g.uid)
    return orderable, unknown


def rid_order_matches(ordered_oldest_first: Sequence[GenerationRef]) -> bool:
    """True when the API rid order (newest first) is exactly the reverse of our order.

    Generations without a rid are ignored. A mismatch is reported as a warning; our order stays canonical.
    """
    with_rid = [g for g in ordered_oldest_first if g.rid]
    by_rid = sorted(with_rid, key=lambda g: rid_rank(g.rid))  # newest first
    return [g.uid for g in by_rid] == [g.uid for g in reversed(with_rid)]
