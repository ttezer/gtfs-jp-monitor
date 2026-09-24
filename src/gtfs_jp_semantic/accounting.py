"""Classification of every raw difference (docs/semantic/01-raw-diff.md, 03-report.md §6).

Each raw change ends up in exactly one bucket: explained by the report, outside the compared days,
or unclassified. Nothing is dropped; the unclassified list shows where the report is silent.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field

OTHER_TOPICS = {
    "fare_attributes.txt": "fares",
    "fare_rules.txt": "fares",
    "calendar_dates.txt": "calendar_exceptions",
    "agency.txt": "agency",
    "agency_jp.txt": "agency",
    "office_jp.txt": "office",
    "translations.txt": "translations",
    "feed_info.txt": "feed_info",
    "shapes.txt": "shapes",
    "transfers.txt": "transfers",
}
# Files whose changes alter trips but are not read by the trip layer; never explained.
UNREAD_SERVICE_FILES = frozenset({"frequencies.txt"})
ROW_FILES = frozenset({"stops.txt", "routes.txt", "trips.txt", "stop_times.txt"})
BULK_KINDS = frozenset({"file_added", "file_removed", "column_added", "column_removed", "file_changed_opaque", "rows_bulk"})


@dataclass
class Evidence:
    """What the report explains, as ids of the source rows."""

    changed_stops: set[str] = field(default_factory=set)  # stop_ids of places added/removed/renamed/moved (both sides)
    changed_routes: set[str] = field(default_factory=set)  # route_ids of lines that are not unchanged
    changed_trips: set[str] = field(default_factory=set)  # trip_ids of compared trips that changed (incl. id-only)
    compared_trips: set[str] = field(default_factory=set)  # trip_ids that ran on a compared day (either side)
    # stop_id -> id of the matched place, per side; equal values mean the stop was only renumbered.
    old_stop_place: dict[str, str] = field(default_factory=dict)
    new_stop_place: dict[str, str] = field(default_factory=dict)


@dataclass
class Accounting:
    explained: list[str] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)
    other: dict[str, list[str]] = field(default_factory=dict)  # topic -> change ids
    files: dict[str, dict[str, int]] = field(default_factory=dict)


def _bucket(change: dict, ev: Evidence) -> tuple[str, str | None]:
    """(bucket, other topic) for one raw change."""
    name, kind = change["file"], change["kind"]
    key = change.get("key") or []
    if name == "calendar.txt":
        return "explained", None  # shown under service days
    if name in OTHER_TOPICS:
        return "explained", OTHER_TOPICS[name]
    if name in UNREAD_SERVICE_FILES:
        return "unclassified", None
    if name in ROW_FILES and kind in BULK_KINDS:
        return "unclassified", None  # structural change of a core file: the row-level layers could not see it
    if name == "stops.txt":
        stop_id = key[0] if key else (change.get("old") or change.get("new") or {}).get("stop_id")
        return ("explained", None) if stop_id in ev.changed_stops else ("unclassified", None)
    if name == "routes.txt":
        route_id = key[0] if key else (change.get("old") or change.get("new") or {}).get("route_id")
        return ("explained", None) if route_id in ev.changed_routes else ("unclassified", None)
    if name == "stop_times.txt" and kind == "field_changed" and change.get("column") == "stop_id":
        target = ev.old_stop_place.get(change["old"])
        if target is not None and target == ev.new_stop_place.get(change["new"]):
            return "explained", None  # same place, new stop id
    if name in ("trips.txt", "stop_times.txt"):
        trip_id = key[0] if key else (change.get("old") or change.get("new") or {}).get("trip_id")
        if trip_id in ev.changed_trips:
            return "explained", None
        if trip_id not in ev.compared_trips:
            return "outside", None
        return "unclassified", None
    return "explained", "other_files"


def classify(raw: dict, ev: Evidence) -> Accounting:
    acc = Accounting()
    per_file: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for change in raw["changes"]:
        bucket, topic = _bucket(change, ev)
        cid = change["id"]
        per_file[change["file"]]["changes"] += 1
        if bucket == "explained":
            acc.explained.append(cid)
            per_file[change["file"]]["classified"] += 1
            if topic:
                acc.other.setdefault(topic, []).append(cid)
        elif bucket == "outside":
            acc.outside.append(cid)
            per_file[change["file"]]["outside_comparison"] += 1
        else:
            acc.unclassified.append(cid)
            per_file[change["file"]]["unclassified"] += 1
    acc.files = {
        name: {k: counts[k] for k in ("changes", "classified", "outside_comparison", "unclassified")}
        for name, counts in sorted(per_file.items())
    }
    return acc
