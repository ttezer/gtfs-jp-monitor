"""Planner report for one pair of publications (docs/semantic/03-report.md).

Joins the raw diff, service days, place, line and trip matches into one
gtfs-jp-semantic-report/1 document, and accounts for every raw difference.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

from . import ENGINE_VERSION
from .accounting import Evidence, classify
from .holidays import DAY_TYPES, HolidayTable, default_table
from .lines import Line, LineMatch, build_lines, match_lines
from .places import PlaceMatch, build_places, match_places, place_of_stop
from .rawdiff import diff_feeds
from .reader import Config, Feed, read_feed
from .report_check import check_report
from .service import build_calendar, choose_comparison
from .trips import Trip, TripPair, build_trips, dominant_pattern, match_trips, pattern_edits

SCHEMA = "gtfs-jp-semantic-report/1"
JP_FILES = frozenset({"agency_jp.txt", "office_jp.txt", "routes_jp.txt", "pattern_jp.txt"})
MAX_MINUTES = 2880
LINE_STATUS_ORDER = ("added", "discontinued", "renamed", "merged", "split", "restructured", "changed", "unchanged")


class ReportInconsistent(ValueError):
    """The assembled report failed check_report; it is never written."""


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _band(minutes: int) -> str:
    h = minutes // 60
    return f"{h:02d}-{h + 1:02d}"


def _time(t: int | None) -> int | None:
    return t if t is not None and 0 <= t <= MAX_MINUTES else None


@dataclass
class _Group:
    """One report line: a line match with the trips of its lines."""

    key: str
    match: LineMatch
    old: list[Line]
    new: list[Line]


class _PlaceIndex:
    """Report places, referenced by trip place ids (new-side ids, or "old:<id>" for unmatched old places)."""

    def __init__(self, matches: list[PlaceMatch]) -> None:
        self.by_new = {m.new.place_id: m for m in matches if m.new}
        self.by_old = {m.old.place_id: m for m in matches if m.old and not m.new}
        self.needed: set[str] = {self.ref(m) for m in matches if m.status != "unchanged"}
        self.index: dict[str, int] = {}

    @staticmethod
    def ref(m: PlaceMatch) -> str:
        return m.new.place_id if m.new else "old:" + m.old.place_id

    def lookup(self, ref: str) -> PlaceMatch:
        return self.by_old[ref[4:]] if ref.startswith("old:") else self.by_new[ref]

    def use(self, refs) -> None:
        self.needed.update(refs)

    def freeze(self) -> list[str]:
        order = sorted(self.needed, key=lambda r: (r.startswith("old:"), r))
        self.index = {r: i for i, r in enumerate(order)}
        return order

    def __getitem__(self, ref: str) -> int:
        return self.index[ref]


def _stop(p) -> dict | None:
    if p is None:
        return None
    return {"stop_id": p.place_id, "name": p.name, "lat": p.lat, "lon": p.lon}


def _groups(line_matches: list[LineMatch], old: dict[str, Line], new: dict[str, Line]) -> list[_Group]:
    groups, seen = [], set()
    for m in line_matches:
        key = "+".join(m.new) if m.new else "+".join(m.old)
        base, n = key, 2
        while key in seen:  # a discontinued old name equal to another group's key
            key, n = f"{base}~{n}", n + 1
        seen.add(key)
        groups.append(_Group(key, m, [old[k] for k in m.old], [new[k] for k in m.new]))
    return groups


def _side(lines: list[Line]) -> dict | None:
    if not lines:
        return None
    names = sorted({n for l in lines for n in l.names} or {l.key for l in lines})
    return {"names": names, "route_ids": sorted({r for l in lines for r in l.route_ids})}


def _rows_for(patterns: list[tuple[str, ...]]) -> tuple[list[str], dict[tuple[str, ...], list[int]]]:
    """Merge place sequences into one row order. Returns rows and, per pattern, its row positions."""
    rows: list[int] = []  # row ids; insertion keeps earlier mappings valid
    row_place: dict[int, str] = {}
    mapping: dict[tuple[str, ...], list[int]] = {}
    for p in patterns:
        current = [row_place[r] for r in rows]
        merged: list[int] = []
        placed: list[int] = []
        for tag, i1, i2, j1, j2 in SequenceMatcher(None, current, p, autojunk=False).get_opcodes():
            if tag == "equal":
                merged += rows[i1:i2]
                placed += rows[i1:i2]
                continue
            merged += rows[i1:i2]  # rows the pattern does not visit stay in place
            new_rows = list(range(len(row_place), len(row_place) + j2 - j1))
            row_place.update(zip(new_rows, p[j1:j2]))
            merged += new_rows
            placed += new_rows
        rows = merged
        mapping[p] = placed
    position = {r: i for i, r in enumerate(rows)}
    return [row_place[r] for r in rows], {p: [position[r] for r in rs] for p, rs in mapping.items()}


def _timetable(trips: list[Trip], places: _PlaceIndex) -> dict | None:
    if not trips:
        return None
    counts = collections.Counter(t.places for t in trips)
    patterns = sorted(counts, key=lambda p: (-counts[p], -len(p), p))
    rows, mapping = _rows_for(patterns)
    places.use(rows)
    table_trips = []
    for t in trips:
        times: list[int | None] = [None] * len(rows)
        for pos, value in zip(mapping[t.places], t.times):
            times[pos] = _time(value)
        table_trips.append({"trip_id": t.trip_id, "times": times})
    return {"places": rows, "trips": table_trips}


def _sort_trips(trips: list[Trip]) -> list[Trip]:
    return sorted(trips, key=lambda t: (t.first_departure if t.first_departure is not None else 10**6, t.trip_id))


def _pair_order(p: TripPair, a: list[Trip], b: list[Trip]) -> tuple:
    """Difference-view columns by earliest departure of either side."""
    times = [t.first_departure for t in (a[p.old] if p.old is not None else None, b[p.new] if p.new is not None else None)
             if t is not None and t.first_departure is not None]
    return (min(times) if times else 10**6, -1 if p.old is None else p.old, -1 if p.new is None else p.new)


def _counts(changes: list[dict], ids: list[str]) -> dict[str, int]:
    wanted = set(ids)
    counts: collections.Counter = collections.Counter()
    for c in changes:
        if c["id"] in wanted:
            counts[{"row_added": "added", "row_removed": "removed", "field_changed": "changed_fields"}.get(c["kind"], c["kind"])] += 1
    return dict(sorted(counts.items()))


def _fares(changes: list[dict]) -> dict:
    fare = [c for c in changes if c["file"] in ("fare_attributes.txt", "fare_rules.txt")]
    attrs = [c for c in fare if c["file"] == "fare_attributes.txt"]
    return {
        "changed": bool(fare),
        "classes_added": sum(c["kind"] == "row_added" for c in attrs),
        "classes_removed": sum(c["kind"] == "row_removed" for c in attrs),
        "prices_changed": len({tuple(c["key"]) for c in attrs if c["kind"] == "field_changed" and c.get("column") == "price"}),
    }


def build_report_from_feeds(old_feed: Feed, new_feed: Feed, *, feed: dict, old_pub: dict, new_pub: dict,
                            config: Config, holidays: HolidayTable, summary_quality: dict | None = None,
                            analysis_key: str | None = None, extra_notes: list[dict] | None = None) -> tuple[dict, dict]:
    """Return (report, raw diff). feed: {org_id, feed_id}; *_pub: publication header fields."""
    raw = diff_feeds(old_feed, new_feed, config)
    ot, nt = old_feed.tables, new_feed.tables
    cfg = config.matching

    old_cal = build_calendar(ot, holidays, config, (_date(old_pub["from_date"]), _date(old_pub.get("to_date"))))
    new_cal = build_calendar(nt, holidays, config, (_date(new_pub["from_date"]), _date(new_pub.get("to_date"))))
    comparison = choose_comparison(old_cal, new_cal, config)

    op, np_ = build_places(ot.get("stops.txt")), build_places(nt.get("stops.txt"))
    place_matches = match_places(op, np_, cfg)
    to_new = {m.old.place_id: m.new.place_id for m in place_matches if m.old and m.new}
    places = _PlaceIndex(place_matches)

    old_lines, new_lines = build_lines(ot, place_of_stop(op)), build_lines(nt, place_of_stop(np_))
    groups = _groups(match_lines(old_lines, new_lines, to_new, cfg), old_lines, new_lines)
    old_route_line = {r: l.key for l in old_lines.values() for r in l.route_ids}
    new_route_line = {r: l.key for l in new_lines.values() for r in l.route_ids}
    group_of_old = {k: g for g in groups for k in g.match.old}
    group_of_new = {k: g for g in groups for k in g.match.new}

    ev = Evidence()
    # trips[group key][direction][day type] = (old trips, new trips)
    trips: dict[str, dict[str, dict[str, tuple[list[Trip], list[Trip]]]]] = collections.defaultdict(
        lambda: collections.defaultdict(dict))
    totals: dict[str, dict[str, int]] = {}
    for dt in DAY_TYPES:
        choice = comparison.day_types[dt]
        old_trips = build_trips(ot, choice.old_services, old_route_line, place_of_stop(op), translate=to_new)
        new_trips = build_trips(nt, choice.new_services, new_route_line, place_of_stop(np_))
        totals[dt] = {"before": len(old_trips), "after": len(new_trips)}
        by: dict[tuple[str, str], tuple[list[Trip], list[Trip]]] = collections.defaultdict(lambda: ([], []))
        for t in old_trips:
            by[(group_of_old[t.line].key, t.direction)][0].append(t)
            ev.compared_trips.add(t.trip_id)
        for t in new_trips:
            by[(group_of_new[t.line].key, t.direction)][1].append(t)
            ev.compared_trips.add(t.trip_id)
        for (gkey, direction), (a, b) in by.items():
            trips[gkey][direction][dt] = (_sort_trips(a), _sort_trips(b))

    shift = cfg["first_last_min_shift"]
    first_last_changed = 0
    line_docs = []
    for g in sorted(groups, key=lambda g: g.key):
        by_dir = trips.get(g.key, {})
        doc_trips: dict[str, dict[str, dict[str, int]]] = {}
        first_last, patterns, timetables = [], [], []
        changed = False
        for direction in sorted(by_dir):
            all_old = [t for a, _ in by_dir[direction].values() for t in a]
            all_new = [t for _, b in by_dir[direction].values() for t in b]
            edits = pattern_edits(dominant_pattern(all_old), dominant_pattern(all_new))
            if edits:
                changed = True
                places.use(p for e in edits for p in e.places)
                patterns.append({"direction": direction, "edits": [{"kind": e.kind, "places": list(e.places)} for e in edits]})
            for dt in DAY_TYPES:
                if dt not in by_dir[direction]:
                    continue
                a, b = by_dir[direction][dt]
                pairs: list[TripPair] = match_trips(a, b, cfg)
                combo_changed = False
                for p in pairs:
                    ta = a[p.old] if p.old is not None else None
                    tb = b[p.new] if p.new is not None else None
                    if p.kind != "exact":
                        combo_changed = True
                    if p.kind != "exact" or ta.trip_id != tb.trip_id:
                        ev.changed_trips.update(t.trip_id for t in (ta, tb) if t is not None)
                if combo_changed:
                    changed = True
                    old_tt, new_tt = _timetable(a, places), _timetable(b, places)
                    order = sorted(pairs, key=lambda p: _pair_order(p, a, b))
                    timetables.append({"direction": direction, "day_type": dt, "old": old_tt, "new": new_tt,
                                       "pairs": [[p.old, p.new] for p in order]})
                bands: dict[str, dict[str, int]] = {}
                for side, side_trips in (("before", a), ("after", b)):
                    for t in side_trips:
                        if t.first_departure is not None:
                            bands.setdefault(_band(t.first_departure), {"before": 0, "after": 0})[side] += 1
                for band, count in bands.items():
                    doc_trips.setdefault(dt, {})[band] = count
                first = {"before": _time(a[0].first_departure) if a else None, "after": _time(b[0].first_departure) if b else None}
                lasts_a = [t.first_departure for t in a if t.first_departure is not None]
                lasts_b = [t.first_departure for t in b if t.first_departure is not None]
                last = {"before": _time(max(lasts_a)) if lasts_a else None, "after": _time(max(lasts_b)) if lasts_b else None}
                first_last.append({"direction": direction, "day_type": dt, "first": first, "last": last})
                if any(x["before"] is not None and x["after"] is not None and abs(x["after"] - x["before"]) >= shift
                       for x in (first, last)):
                    first_last_changed += 1
        relation = g.match.relation
        status = ("changed" if changed else "unchanged") if relation == "same" else relation
        if status != "unchanged":
            ev.changed_routes.update(r for l in g.old + g.new for r in l.route_ids)
        else:
            # Same line, same service: routes whose id changed are renumbering.
            ev.changed_routes.update({r for l in g.old for r in l.route_ids} ^ {r for l in g.new for r in l.route_ids})
            doc_trips, first_last = {}, []
        line_docs.append({
            "key": g.key,
            "status": status,
            "old": _side(g.old),
            "new": _side(g.new),
            "related": [],
            "match": None if g.match.method is None else {"method": g.match.method, "confidence": g.match.confidence},
            "trips": {dt: dict(sorted(b.items())) for dt, b in sorted(doc_trips.items())},
            "first_last": first_last,
            "patterns": patterns,
            "timetables": timetables,
        })

    for m in place_matches:
        if m.status != "unchanged" or (m.old and m.new and m.old.place_id != m.new.place_id):
            ev.changed_stops.update(m.old.members if m.old else ())
            ev.changed_stops.update(m.new.members if m.new else ())

    # Lines serving each place, in report keys.
    serving: dict[str, set[str]] = collections.defaultdict(set)
    for g in groups:
        for l in g.new:
            for p in l.places:
                serving[p].add(g.key)
        for l in g.old:
            for p in l.places:
                serving[to_new.get(p, "old:" + p)].add(g.key)

    order = places.freeze()
    place_docs = []
    for ref in order:
        m = places.lookup(ref)
        place_docs.append({
            "status": m.status,
            "old": _stop(m.old),
            "new": _stop(m.new),
            "match": None if m.method is None else {"method": m.method, "confidence": m.confidence},
            "moved_m": m.distance_m if m.status in ("moved", "renamed_moved") else None,
            "lines": sorted(serving.get(ref, ())),
        })
    for line in line_docs:
        for pattern in line["patterns"]:
            for edit in pattern["edits"]:
                edit["places"] = [places[r] for r in edit["places"]]
        for table in line["timetables"]:
            for side in ("old", "new"):
                if table[side] is not None:
                    table[side]["places"] = [places[r] for r in table[side]["places"]]

    ev.old_stop_place = {sid: to_new[pid] for sid, pid in place_of_stop(op).items() if pid in to_new}
    ev.new_stop_place = place_of_stop(np_)
    acc = classify(raw, ev)
    notes: list[dict] = []
    old_jp = sorted(n for n in ot if n in JP_FILES)
    new_jp = sorted(n for n in nt if n in JP_FILES)
    if old_jp != new_jp:
        notes.append({"code": "FORMAT_DIFFERENCE", "old_files": old_jp, "new_files": new_jp})
    notes.extend(extra_notes or [])
    if not any(c.old_date and c.new_date for c in comparison.day_types.values()):
        notes.append({"code": "NO_COMMON_DAY_TYPE"})
    if acc.outside:
        notes.append({"code": "OUTSIDE_COMPARISON_PRESENT"})

    status_counts = collections.Counter(l["status"] for l in line_docs)
    place_status = collections.Counter(m.status for m in place_matches)
    report = {
        "schema": SCHEMA,
        "header": {
            "feed": {"org_id": feed["org_id"], "feed_id": feed["feed_id"]},
            "old": {k: old_pub.get(k) for k in ("uid", "from_date", "to_date", "published_at")} | {"memo": old_pub.get("memo") or ""},
            "new": {k: new_pub.get(k) for k in ("uid", "from_date", "to_date", "published_at")} | {"memo": new_pub.get("memo") or ""},
            "comparison": {
                "mode": comparison.mode,
                "day_types": {dt: {"old_date": c.old_date.isoformat() if c.old_date else None,
                                   "new_date": c.new_date.isoformat() if c.new_date else None}
                              for dt, c in comparison.day_types.items()},
            },
            "notes": notes,
            "engine": {"version": ENGINE_VERSION, "config": config.as_dict(), "holidays_version": holidays.version},
            "coverage": {"raw_total": len(raw["changes"]), "explained": len(acc.explained),
                         "outside_comparison": len(acc.outside), "unclassified": len(acc.unclassified)},
        },
        "summary": {
            "lines": {s: status_counts[s] for s in LINE_STATUS_ORDER},
            "places": {
                "added": place_status["added"],
                "removed": place_status["removed"],
                "renamed": place_status["renamed"] + place_status["renamed_moved"],
                "moved": place_status["moved"] + place_status["renamed_moved"],
            },
            "trips_by_day_type": totals,
            "first_last_changed": first_last_changed,
            "fares": _fares(raw["changes"]),
            "quality": summary_quality,
        },
        "service_days": {
            "day_types": {dt: {"active_days": {"before": old_cal.active_days(dt), "after": new_cal.active_days(dt)}}
                          for dt in DAY_TYPES},
            "periods": {side: [{"day_type": p.day_type, "start": p.start.isoformat(), "end": p.end.isoformat()}
                               for p in cal.periods] for side, cal in (("old", old_cal), ("new", new_cal))},
            "special_days": {side: sorted(d.isoformat() for d in cal.special) for side, cal in (("old", old_cal), ("new", new_cal))},
        },
        "places": place_docs,
        "lines": line_docs,
        "other": [{"topic": topic, "counts": _counts(raw["changes"], ids), "evidence": ids}
                  for topic, ids in sorted(acc.other.items())],
        "accounting": {"files": acc.files, "unclassified": acc.unclassified},
        "quality": {"analysis_key": analysis_key} if analysis_key else None,
    }
    problems = check_report(report)
    if problems:
        raise ReportInconsistent("; ".join(problems[:10]))
    return report, raw


def build_report(old_zip: Path, new_zip: Path, *, config: Config | None = None, holidays: HolidayTable | None = None,
                 **kwargs) -> tuple[dict, dict]:
    config = config or Config.load()
    return build_report_from_feeds(read_feed(old_zip, config), read_feed(new_zip, config), config=config,
                                   holidays=holidays or default_table(), **kwargs)
