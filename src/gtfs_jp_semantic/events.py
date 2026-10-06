"""Semantic events and id mappings of a change report (docs/semantic/03-report.md, "Events").

The report's sections describe places, lines and their trips. With `report.events`, the report also
lists every conclusion as one event of a fixed type, each with the raw differences (Part 1) that
caused it, and the ids that changed between the publications as old -> new mappings. Events are
derived from facts the report builder gathered; they add no new judgement.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

PLACE_EVENTS = {"added": ("STOP_ADDED",), "removed": ("STOP_REMOVED",), "renamed": ("STOP_RENAMED",),
                "moved": ("STOP_MOVED",), "renamed_moved": ("STOP_RENAMED", "STOP_MOVED")}
LINE_EVENTS = {"added": "ROUTE_ADDED", "discontinued": "ROUTE_DISCONTINUED", "renamed": "ROUTE_RENAMED",
               "merged": "ROUTE_MERGED", "split": "ROUTE_SPLIT", "restructured": "ROUTE_RESTRUCTURED"}
EDIT_EVENTS = {"extended": "PATTERN_EXTENDED", "shortened": "PATTERN_SHORTENED", "inserted": "STOP_INSERTED",
               "removed": "STOP_SKIPPED", "detour_added": "DETOUR_ADDED", "detour_removed": "DETOUR_REMOVED",
               "reordered": "STOPS_REORDERED", "shifted": "DETOUR_SHIFTED"}
EVENT_TYPES = (*sorted({t for ts in PLACE_EVENTS.values() for t in ts}), *sorted(LINE_EVENTS.values()),
               *sorted(EDIT_EVENTS.values()), "SERVICE_INCREASED", "SERVICE_REDUCED", "TRIPS_RETIMED",
               "FIRST_LAST_CHANGED", "TRIPS_MOVED", "FARE_CHANGED", "OTHER_CHANGED")

# Files whose rows name an entity, and the column that names it.
_ENTITY_COLUMN = {"stops.txt": "stop_id", "routes.txt": "route_id", "trips.txt": "trip_id",
                  "stop_times.txt": "trip_id", "frequencies.txt": "trip_id",
                  "calendar.txt": "service_id", "calendar_dates.txt": "service_id"}


@dataclass
class Facts:
    """What the report builder found, by source ids. Refs of places are report place indexes."""

    places: list[tuple[int, str, tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)
    lines: list[tuple[str, str, tuple[str, ...], tuple[str, ...], set[str]]] = field(default_factory=list)
    # (line, direction, edit kind, place indexes, trips showing it or None, trip ids)
    edits: list[tuple[str, str, str, list[int], int | None, set[str]]] = field(default_factory=list)
    # (line, day type, trips before, trips after, trip ids not paired, their service ids)
    service: list[tuple[str, str, int, int, set[str], set[str]]] = field(default_factory=list)
    retimed: list[tuple[str, str, int, set[str]]] = field(default_factory=list)  # (line, day type, pairs, trip ids)
    first_last: list[tuple[str, str, str, set[str]]] = field(default_factory=list)  # (line, direction, day type, trip ids)
    # (old line, new line, day type) -> [moved trips, their old and new trip ids]
    moves: dict[tuple[str, str, str], list] = field(default_factory=lambda: defaultdict(lambda: [0, set()]))
    other: dict[str, list[str]] = field(default_factory=dict)  # topic -> raw change ids
    trip_ids: set[tuple[str, str]] = field(default_factory=set)  # (old trip_id, new trip_id) of paired trips
    trip_services: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # trip_id -> service_ids (either side)


def evidence_index(raw: dict) -> dict[tuple[str, str], list[str]]:
    """Raw change ids by (file, id of the entity the row belongs to)."""
    index: dict[tuple[str, str], list[str]] = defaultdict(list)
    for change in raw["changes"]:
        column = _ENTITY_COLUMN.get(change["file"])
        if column is None:
            continue
        entity = None
        if change.get("key"):
            entity = change["key"][0]
        else:  # whole-row comparison: the entity is a value of the row
            row = change.get("new") or change.get("old")
            if isinstance(row, dict):
                entity = row.get(column)
        if entity:
            index[(change["file"], entity)].append(change["id"])
    return index


def ranges(ids) -> list[str]:
    """Raw change ids as runs: "c0000012-c0000340" for consecutive ids, in order."""
    nums = sorted({int(i[1:]) for i in ids})
    out: list[str] = []
    start = prev = None
    for n in nums:
        if prev is not None and n == prev + 1:
            prev = n
            continue
        if start is not None:
            out.append(f"c{start:07d}" if start == prev else f"c{start:07d}-c{prev:07d}")
        start = prev = n
    if start is not None:
        out.append(f"c{start:07d}" if start == prev else f"c{start:07d}-c{prev:07d}")
    return out


def build_events(facts: Facts, raw: dict) -> list[dict]:
    index = evidence_index(raw)

    def ids(files: tuple[str, ...], entities) -> list[str]:
        return [i for f in files for e in entities for i in index.get((f, e), ())]

    def ev(files: tuple[str, ...], entities) -> list[str]:
        return ranges(ids(files, entities))

    def trip_ev(files: tuple[str, ...], trips) -> list[str]:
        """Rows of the trips; when they did not change, the difference is in which services run on
        the compared days, so the calendar rows of their services."""
        found = ids(files, trips)
        if not found:
            found = ids(("calendar.txt", "calendar_dates.txt"), {s for t in trips for s in facts.trip_services.get(t, ())})
        return ranges(found)

    events: list[dict] = []
    for ref, status, old_members, new_members in facts.places:
        for kind in PLACE_EVENTS.get(status, ()):
            events.append({"type": kind, "place": ref, "evidence": ev(("stops.txt",), {*old_members, *new_members})})
    for key, status, old_routes, new_routes, trips in facts.lines:
        kind = LINE_EVENTS.get(status)
        if kind is None:
            continue
        files = ("routes.txt", "trips.txt") if status in ("added", "discontinued") else ("routes.txt",)
        entities = {*old_routes, *new_routes} | (trips if status in ("added", "discontinued") else set())
        events.append({"type": kind, "line": key, "evidence": ev(files, entities)})
    for key, direction, kind, places, count, trips in facts.edits:
        events.append({"type": EDIT_EVENTS[kind], "line": key, "direction": direction, "places": places,
                       "count": count, "evidence": trip_ev(("stop_times.txt",), trips)})
    for key, day_type, before, after, trips, services in facts.service:
        if before == after:
            continue
        events.append({"type": "SERVICE_INCREASED" if after > before else "SERVICE_REDUCED", "line": key,
                       "day_type": day_type, "before": before, "after": after,
                       "evidence": ranges(ids(("trips.txt", "stop_times.txt", "frequencies.txt"), trips)
                                          + ids(("calendar.txt", "calendar_dates.txt"), services))})
    for key, day_type, count, trips in facts.retimed:
        events.append({"type": "TRIPS_RETIMED", "line": key, "day_type": day_type, "count": count,
                       "evidence": trip_ev(("stop_times.txt", "frequencies.txt"), trips)})
    for key, direction, day_type, trips in facts.first_last:
        events.append({"type": "FIRST_LAST_CHANGED", "line": key, "direction": direction, "day_type": day_type,
                       "evidence": trip_ev(("stop_times.txt", "frequencies.txt"), trips)})
    for (old_line, new_line, day_type), (count, trips) in sorted(facts.moves.items()):
        events.append({"type": "TRIPS_MOVED", "line": old_line, "to_line": new_line, "day_type": day_type,
                       "count": count, "evidence": trip_ev(("trips.txt", "stop_times.txt"), trips)})
    for topic, ids in sorted(facts.other.items()):
        if topic == "fares":
            events.append({"type": "FARE_CHANGED", "evidence": ranges(ids)})
        else:
            events.append({"type": "OTHER_CHANGED", "topic": topic, "evidence": ranges(ids)})
    return events


def build_mappings(facts: Facts) -> dict:
    """Ids that changed between the publications, old -> new: places (their stops), lines (their
    routes) and paired trips. An id not listed kept its value, or is in an added or removed event."""
    stops = [{"old": list(o), "new": list(n)} for _, status, o, n in facts.places
             if o and n and set(o) != set(n)]
    routes = [{"line": key, "old": list(o), "new": list(n)} for key, status, o, n, _ in facts.lines
              if o and n and set(o) != set(n)]
    trips = [[o, n] for o, n in sorted(facts.trip_ids) if o != n]
    return {"stops": sorted(stops, key=lambda m: (m["old"], m["new"])), "routes": sorted(routes, key=lambda m: m["line"]),
            "trips": trips}
