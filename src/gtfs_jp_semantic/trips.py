"""Trips of a typical day, pattern edits and trip matching (docs/semantic/02-matching.md §5-6).

Old trips are expressed in the new side's place ids (via place matches) so that renumbered stops
compare equal. Unmatched old places keep a distinct "old:" id and never equal a new place.
"""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from difflib import SequenceMatcher

from .reader import Table


@dataclass(frozen=True)
class Trip:
    trip_id: str
    line: str
    direction: str
    places: tuple[str, ...]
    times: tuple[int | None, ...]  # minutes after midnight per place

    @property
    def first_departure(self) -> int | None:
        return next((t for t in self.times if t is not None), None)


@dataclass(frozen=True)
class TripPair:
    old: int | None  # index into the old trip list
    new: int | None
    kind: str  # exact | retimed | rerouted | retimed_rerouted | removed | added


@dataclass(frozen=True)
class PatternEdit:
    kind: str  # extended | shortened | inserted | removed | detour_added | detour_removed | reordered
    places: tuple[str, ...]


def parse_minutes(value: str) -> int | None:
    parts = (value or "").strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    h, m, _ = (int(p) for p in parts)
    return h * 60 + m if m < 60 else None


def _rows(table: Table | None) -> list[dict[str, str]]:
    if table is None or table.status != "ok":
        return []
    return [dict(zip(table.header, r)) for r in table.rows]


def direction_key(direction_id: str, places: tuple[str, ...]) -> str:
    if direction_id in ("0", "1"):
        return direction_id
    ends = f"{places[0] if places else ''}>{places[-1] if places else ''}"
    return hashlib.sha256(ends.encode("utf-8")).hexdigest()[:8]


def build_trips(tables: dict[str, Table], services: frozenset[str], route_line: dict[str, str],
                place_of_stop: dict[str, str], translate: dict[str, str] | None = None) -> list[Trip]:
    """Trips running on a day with `services`. translate maps this side's place ids to new-side ids."""
    trips = {t["trip_id"]: t for t in _rows(tables.get("trips.txt"))
             if t.get("trip_id") and t.get("service_id") in services and t.get("route_id") in route_line}
    calls: dict[str, list[tuple[int, str, int | None]]] = {}
    for st in _rows(tables.get("stop_times.txt")):
        tid = st.get("trip_id", "")
        if tid not in trips:
            continue
        try:
            seq = int(st.get("stop_sequence", ""))
        except ValueError:
            continue
        place = place_of_stop.get(st.get("stop_id", ""))
        if place is None:
            continue
        if translate is not None:
            place = translate.get(place, "old:" + place)
        t = parse_minutes(st.get("departure_time", "")) if st.get("departure_time") else None
        if t is None:
            t = parse_minutes(st.get("arrival_time", ""))
        calls.setdefault(tid, []).append((seq, place, t))
    result = []
    for tid, trip in trips.items():
        seq = sorted(calls.get(tid, []))
        if not seq:
            continue
        places = tuple(p for _, p, _ in seq)
        result.append(Trip(tid, route_line[trip["route_id"]], direction_key((trip.get("direction_id") or "").strip(), places),
                           places, tuple(t for _, _, t in seq)))
    return sorted(result, key=lambda x: (x.line, x.direction, x.first_departure if x.first_departure is not None else 10**6, x.trip_id))


def _lcs_ratio(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    if not a or not b:
        return 0.0
    matched = sum(block.size for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return matched / max(len(a), len(b))


def _time_map(trip: Trip) -> dict[str, int]:
    times: dict[str, int] = {}
    for p, t in zip(trip.places, trip.times):
        if t is not None:
            times.setdefault(p, t)  # first call at a place (loops visit it twice)
    return times


def _shifts(a: Trip, times_b: dict[str, int]) -> list[int]:
    return [abs(t - times_b[p]) for p, t in zip(a.places, a.times) if t is not None and p in times_b]


def match_trips(old: list[Trip], new: list[Trip], cfg: dict) -> list[TripPair]:
    """Match trips of one line group, direction and day type."""
    cap = cfg["trip_max_shift_min"]
    max_cost = cfg["trip_max_cost"]
    min_sim = cfg["pattern_min_similarity"]
    pairs: dict[int, tuple[int, str]] = {}
    used: set[int] = set()
    new_times = [_time_map(b) for b in new]

    # 1. exact: same places and times; prefer the same trip_id, then departure order.
    exact = [(0 if a.trip_id == b.trip_id else 1, a.first_departure or 0, i, j)
             for i, a in enumerate(old) for j, b in enumerate(new) if a.places == b.places and a.times == b.times]
    for _, _, i, j in sorted(exact):
        if i not in pairs and j not in used:
            pairs[i] = (j, "exact")
            used.add(j)

    # 2. lowest-cost assignment for the rest.
    candidates = []
    for i, a in enumerate(old):
        if i in pairs:
            continue
        for j, b in enumerate(new):
            if j in used:
                continue
            sim = _lcs_ratio(a.places, b.places)
            diffs = _shifts(a, new_times[j])
            if not diffs or sim < min_sim:
                continue
            shift = statistics.median(diffs)
            if shift > cap:
                continue
            cost = round(shift / cap + (1.0 - sim), 6)
            if cost <= max_cost:
                candidates.append((cost, a.first_departure or 0, b.first_departure or 0, a.trip_id, b.trip_id, i, j))
    for *_, i, j in sorted(candidates):
        if i in pairs or j in used:
            continue
        a, b = old[i], new[j]
        rerouted = a.places != b.places
        retimed = a.times != b.times if not rerouted else any(_shifts(a, new_times[j]))
        kind = "retimed_rerouted" if rerouted and retimed else "rerouted" if rerouted else "retimed"
        pairs[i] = (j, kind)
        used.add(j)

    result = [TripPair(i, pairs[i][0], pairs[i][1]) if i in pairs else TripPair(i, None, "removed") for i in range(len(old))]
    result += [TripPair(None, j, "added") for j in range(len(new)) if j not in used]
    return result


def _containment(a: tuple[str, ...], b: tuple[str, ...]) -> float:
    """Share of the shorter place sequence found, in order, in the longer one."""
    if not a or not b:
        return 0.0
    matched = sum(block.size for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks())
    return matched / min(len(a), len(b))


def match_moved_trips(old: list[Trip], new: list[Trip], cfg: dict) -> list[TripPair]:
    """Pair trips left unmatched in their own line with trips of other lines or directions.

    A route variant of the same corridor keeps one end and most of its places: the shorter
    sequence must lie largely inside the longer one and share its first or last place, and the
    trip must run at about the same time. Stricter than match_trips, since lines differ.
    """
    cap = cfg["cross_line_max_shift_min"]
    min_share = cfg["cross_line_min_containment"]
    new_times = [_time_map(b) for b in new]
    candidates = []
    for i, a in enumerate(old):
        for j, b in enumerate(new):
            if not a.places or not b.places or (a.places[0] != b.places[0] and a.places[-1] != b.places[-1]):
                continue
            share = _containment(a.places, b.places)
            diffs = _shifts(a, new_times[j])
            if share < min_share or not diffs:
                continue
            shift = statistics.median(diffs)
            if shift > cap:
                continue
            cost = round(shift / cap + (1.0 - share), 6)
            candidates.append((cost, a.first_departure or 0, b.first_departure or 0, a.trip_id, b.trip_id, i, j))
    pairs: dict[int, tuple[int, str]] = {}
    used: set[int] = set()
    for *_, i, j in sorted(candidates):
        if i in pairs or j in used:
            continue
        a, b = old[i], new[j]
        rerouted = a.places != b.places
        retimed = a.times != b.times if not rerouted else any(_shifts(a, new_times[j]))
        pairs[i] = (j, "retimed_rerouted" if rerouted and retimed else "rerouted" if rerouted else "retimed" if retimed else "exact")
        used.add(j)
    result = [TripPair(i, pairs[i][0], pairs[i][1]) if i in pairs else TripPair(i, None, "removed") for i in range(len(old))]
    result += [TripPair(None, j, "added") for j in range(len(new)) if j not in used]
    return result


def dominant_pattern(trips: list[Trip]) -> tuple[str, ...]:
    """Most frequent place sequence (ties: the longest, then lexical order)."""
    counts: dict[tuple[str, ...], int] = {}
    for t in trips:
        counts[t.places] = counts.get(t.places, 0) + 1
    if not counts:
        return ()
    return max(counts, key=lambda p: (counts[p], len(p), tuple(reversed(p))))


def pattern_edits(old: tuple[str, ...], new: tuple[str, ...]) -> list[PatternEdit]:
    """Describe how the new place sequence differs from the old one."""
    edits: list[PatternEdit] = []
    if not old or not new:
        return edits
    ops = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            continue
        at_start, at_end_old, at_end_new = i1 == 0 and j1 == 0, i2 == len(old), j2 == len(new)
        at_edge = at_start or (at_end_old and at_end_new)
        if tag in ("delete", "replace") and i2 > i1:
            removed = tuple(old[i1:i2])
            kind = "shortened" if at_edge and tag == "delete" else ("removed" if len(removed) == 1 else "detour_removed")
            edits.append(PatternEdit(kind, removed))
        if tag in ("insert", "replace") and j2 > j1:
            added = tuple(new[j1:j2])
            kind = "extended" if at_edge and tag == "insert" else ("inserted" if len(added) == 1 else "detour_added")
            edits.append(PatternEdit(kind, added))
    return _collapse_reordered(edits)


_OUT = ("shortened", "removed", "detour_removed")
_IN = ("extended", "inserted", "detour_added")


def _collapse_reordered(edits: list[PatternEdit]) -> list[PatternEdit]:
    """A place that leaves the sequence in one edit and comes back in another only moved:
    report it once as `reordered` (in new-side order) instead of a removal plus an insertion."""
    gone = {p for e in edits if e.kind in _OUT for p in e.places}
    came = [p for e in edits if e.kind in _IN for p in e.places]
    moved = [p for p in came if p in gone]
    if not moved:
        return edits
    keep = []
    for e in edits:
        rest = tuple(p for p in e.places if p not in moved)
        if rest:
            keep.append(PatternEdit(e.kind, rest))
    return keep + [PatternEdit("reordered", tuple(dict.fromkeys(moved)))]
