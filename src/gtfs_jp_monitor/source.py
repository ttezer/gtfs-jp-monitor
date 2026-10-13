"""What a feed source hands to the catalog sync (data-model §7).

gtfs-data.jp is one source (gtfsdatajp.py); another publisher plugs in by returning the same
records. Values are untrusted until the source has validated them: identifiers must already
pass ids.is_path_id / ids.is_uid, and download URLs must be checked against the source's host.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ApiError(RuntimeError):
    """The source answered with an unusable status or body."""


@dataclass(frozen=True)
class Rejected:
    """A source record that was skipped instead of trusted."""

    code: str
    org_id: str | None
    feed_id: str | None
    uid: str | None
    detail: str


@dataclass(frozen=True)
class FeedRecord:
    org_id: str
    feed_id: str
    feed_name: str
    organization_name: str
    feed_pref_id: int | None
    license_raw: str
    license_url: str | None
    is_discontinued: bool
    discontinued_date: str | None
    last_published_at: str | None
    latest_feed_start_date: str | None
    latest_feed_end_date: str | None
    memo: str


@dataclass(frozen=True)
class GenerationRecord:
    uid: str
    rid: str | None  # gtfs-data.jp position (current, prev_N, next_N); None when the source has none
    gtfs_url: str
    from_date: str | None
    to_date: str | None
    published_at: str | None
    memo: str


@dataclass(frozen=True)
class FeedGenerations:
    org_id: str
    feed_id: str
    license_raw: str
    reported_max_prev: int | None
    reported_max_next: int | None
    generations: tuple[GenerationRecord, ...]
    rejected: tuple[Rejected, ...] = field(default=())

    @property
    def complete(self) -> bool:
        """True when the returned prev_/next_ counts match what the source says exists."""
        prev = sum(1 for g in self.generations if (g.rid or "").startswith("prev_"))
        nxt = sum(1 for g in self.generations if (g.rid or "").startswith("next_"))
        rejected_prev = sum(1 for r in self.rejected if r.detail.startswith("rid=prev_"))
        rejected_next = sum(1 for r in self.rejected if r.detail.startswith("rid=next_"))
        return (self.reported_max_prev is None or prev + rejected_prev == self.reported_max_prev) and (
            self.reported_max_next is None or nxt + rejected_next == self.reported_max_next
        )


class CatalogSource(Protocol):
    def list_feeds(self) -> tuple[list[FeedRecord], list[Rejected]]: ...

    def get_generations(self, org_id: str, feed_id: str) -> FeedGenerations:
        """Raises ApiError when the feed cannot be read; its previous catalog entries are then kept."""
        ...
