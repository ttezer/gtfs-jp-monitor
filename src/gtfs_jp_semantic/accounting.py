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
# Columns the place, line and trip layers read. A change elsewhere in these files is an attribute
# change (headsign, pickup rule, shape link, codes, ...), reported under Other as attributes.
CORE_COLUMNS = {
    "stops.txt": frozenset({"stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station", "location_type"}),
    "routes.txt": frozenset({"route_id", "route_short_name", "route_long_name"}),
    "trips.txt": frozenset({"trip_id", "route_id", "service_id", "direction_id"}),
    "stop_times.txt": frozenset({"trip_id", "stop_sequence", "stop_id", "arrival_time", "departure_time"}),
}


@dataclass
class Evidence:
    """What the report explains, as ids of the source rows."""

    changed_stops: set[str] = field(default_factory=set)  # stop_ids of places added/removed/renamed/moved (both sides)
    changed_routes: set[str] = field(default_factory=set)  # route_ids of lines that are not unchanged
    changed_trips: set[str] = field(default_factory=set)  # trip_ids of compared trips that changed (incl. id-only)
    # trip_ids paired exactly with themselves: same places and times, so core stop_times changes
    # can only be a renumbered stop_sequence
    same_trips: set[str] = field(default_factory=set)
    # trip_ids that ran on any date both publications cover; the per-date comparison accounts for them
    date_trips: set[str] = field(default_factory=set)
    # route_id (either side) -> report line key; a trip moving between routes of one line is renumbering
    route_line: dict[str, str] = field(default_factory=dict)
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


def _row_value(change: dict, column: str) -> str | None:
    key = change.get("key")
    if key:
        return key[0]
    return (change.get("old") or change.get("new") or {}).get(column)


def _row_file_bucket(change: dict, ev: Evidence) -> tuple[str, str | None]:
    name, kind, column = change["file"], change["kind"], change.get("column")
    core = CORE_COLUMNS[name]
    if kind in ("column_added", "column_removed"):
        return ("unclassified", None) if column in core else ("explained", "attributes")
    if kind in BULK_KINDS:
        return "unclassified", None  # file-level change of a core file: the row-level layers could not see it
    attribute = kind == "field_changed" and column not in core
    if name == "stops.txt":
        if _row_value(change, "stop_id") in ev.changed_stops:
            return "explained", None
        if attribute or (kind == "field_changed" and column in ("stop_lat", "stop_lon")):
            return "explained", "attributes"  # codes, descriptions, or a move below stop_moved_min_m
        return "unclassified", None
    if name == "routes.txt":
        if _row_value(change, "route_id") in ev.changed_routes:
            return "explained", None
        return ("explained", "attributes") if attribute else ("unclassified", None)
    if name == "stop_times.txt" and kind == "field_changed" and column == "stop_id":
        target = ev.old_stop_place.get(change["old"])
        if target is not None and target == ev.new_stop_place.get(change["new"]):
            return "explained", None  # same place, new stop id
    trip_id = _row_value(change, "trip_id")
    if trip_id not in ev.compared_trips and trip_id not in ev.date_trips:
        return "outside", None
    if attribute:
        return "explained", "attributes"
    if trip_id in ev.changed_trips:
        return "explained", None
    if trip_id not in ev.compared_trips:
        return "explained", None  # ran on a shared date only: listed under date changes, or no change there
    if name == "trips.txt" and kind == "field_changed" and column == "route_id" and trip_id in ev.same_trips:
        line = ev.route_line.get(change.get("old"))
        if line is not None and line == ev.route_line.get(change.get("new")):
            return "explained", None  # same trip, renumbered route of the same line
    if name == "stop_times.txt" and trip_id in ev.same_trips:
        return "explained", None  # same places and times: stop_sequence was renumbered
    return "unclassified", None


def _bucket(change: dict, ev: Evidence) -> tuple[str, str | None]:
    """(bucket, other topic) for one raw change."""
    name, kind = change["file"], change["kind"]
    if kind == "field_changed" and _same_value(change.get("old"), change.get("new")):
        return "explained", "formatting"  # the value did not change, only how it is written
    if name == "calendar.txt":
        return "explained", None  # shown under service days
    if name in OTHER_TOPICS:
        return "explained", OTHER_TOPICS[name]
    if name in UNREAD_SERVICE_FILES:
        return "unclassified", None
    if name in ROW_FILES:
        return _row_file_bucket(change, ev)
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
