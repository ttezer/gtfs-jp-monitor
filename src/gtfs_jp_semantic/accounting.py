"""Classification of every raw difference (docs/semantic/01-raw-diff.md, 03-report.md §6).

Each raw change ends up in exactly one bucket: explained by the report, outside the compared days,
or unclassified. Nothing is dropped; the unclassified list shows where the report is silent.
"""

from __future__ import annotations

import math
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


def _same_value(old: object, new: object) -> bool:
    """Both sides write the same number (140.5208490 / 140.520849) or the same time (7:30:00 / 07:30:00)."""
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    try:
        a, b = float(old), float(new)
        if math.isfinite(a) and math.isfinite(b):
            return a == b
    except ValueError:
        pass
    pa, pb = old.strip().split(":"), new.strip().split(":")
    if len(pa) == len(pb) == 3 and all(x.isdigit() for x in pa + pb):
        return [int(x) for x in pa] == [int(x) for x in pb]
    return False


def _bucket(change: dict, ev: Evidence) -> tuple[str, str | None]:
    """(bucket, other topic) for one raw change."""
    name, kind = change["file"], change["kind"]
    key = change.get("key") or []
    if kind == "field_changed" and _same_value(change.get("old"), change.get("new")):
        return "explained", "formatting"  # the value did not change, only how it is written
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
    for change in raw["changes"]:
        bucket, topic = _bucket(change, ev)
        cid = change["id"]
        if bucket == "explained":
            acc.explained.append(cid)
            if topic:
                acc.other.setdefault(topic, []).append(cid)
        elif bucket == "outside":
            acc.outside.append(cid)
        else:
            acc.unclassified.append(cid)
    return acc
