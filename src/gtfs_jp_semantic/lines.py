"""Lines and their matching between two publications (docs/semantic/02-matching.md §4).

A line groups the routes a passenger sees as one: same normalised short name, else long name,
else route_id; a configured family pattern can take the line's name out of a variant's name
("［市振線］早朝便（…）" -> "市振線"). Lines match by equal name first (configured parts such as a
line code prefix removed), then by the places their trips serve. Groups of matched lines are
reported with their shape (1:1, merge, split, restructure); groups larger than the configured
limit are not interpreted and stay unmatched.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .reader import Table


@dataclass(frozen=True)
class Line:
    key: str
    names: tuple[str, ...]
    route_ids: tuple[str, ...]
    places: frozenset[str]  # place ids served by the line's trips


@dataclass(frozen=True)
class LineMatch:
    old: tuple[str, ...]  # line keys
    new: tuple[str, ...]
    relation: str  # same | renamed | merged | split | restructured | discontinued | added
    method: str | None  # same_line_name | served_places
    confidence: float | None


def _rows(table: Table | None) -> list[dict[str, str]]:
    if table is None or table.status != "ok":
        return []
    return [dict(zip(table.header, r)) for r in table.rows]


def line_key(route: dict[str, str], family_patterns: list[re.Pattern] = ()) -> str:
    """The first non-empty of short name, long name and route_id, normalised. A family pattern that
    matches the name replaces it with the pattern's first group, so trip variants of one line
    published as separate routes form one line."""
    for field in ("route_short_name", "route_long_name", "route_id"):
        value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", route.get(field, "") or ""))
        if value:
            for pattern in family_patterns:
                found = pattern.search(value)
                if found and found.group(1):
                    return found.group(1)
            return value
    return ""


def _patterns(cfg: dict | None, name: str) -> list[re.Pattern]:
    return [re.compile(p) for p in (cfg or {}).get(name, [])]


def build_lines(tables: dict[str, Table], place_of_stop: dict[str, str], cfg: dict | None = None) -> dict[str, Line]:
    routes = {r.get("route_id", ""): r for r in _rows(tables.get("routes.txt")) if r.get("route_id")}
    trip_route = {t.get("trip_id", ""): t.get("route_id", "") for t in _rows(tables.get("trips.txt"))}
    served: dict[str, set[str]] = {}
    stop_times = tables.get("stop_times.txt")
    if stop_times is not None and stop_times.status == "ok" and {"trip_id", "stop_id"} <= set(stop_times.header):
        ti, si = stop_times.header.index("trip_id"), stop_times.header.index("stop_id")
        for row in stop_times.rows:
            route_id = trip_route.get(row[ti])
            place = place_of_stop.get(row[si])
            if route_id and place:
                served.setdefault(route_id, set()).add(place)
    families = _patterns(cfg, "line_family_patterns")
    groups: dict[str, list[str]] = {}
    for route_id, route in routes.items():
        key = line_key(route, families)
        if key:
            groups.setdefault(key, []).append(route_id)
    lines: dict[str, Line] = {}
    for key, route_ids in groups.items():
        ids = tuple(sorted(route_ids))
        names = sorted({n for rid in ids for n in (routes[rid].get("route_short_name", ""), routes[rid].get("route_long_name", "")) if n})
        places = frozenset().union(*(served.get(rid, set()) for rid in ids))
        lines[key] = Line(key, tuple(names), ids, places)
    return lines


def _overlap(a: frozenset[str], b: frozenset[str]) -> float:
    """Overlap coefficient: shared places over the smaller set, so a line absorbed into another
    (merge) or divided into several (split) still scores high. Only lines without a same-name
    match reach this step, which keeps short lines inside trunk corridors from matching."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _shape(olds: set[str] | tuple[str, ...], news: set[str] | tuple[str, ...]) -> str:
    return ("renamed" if len(olds) == 1 and len(news) == 1 else
            "merged" if len(news) == 1 else
            "split" if len(olds) == 1 else "restructured")


def match_lines(old: dict[str, Line], new: dict[str, Line], place_map: dict[str, str], cfg: dict) -> list[LineMatch]:
    """place_map: old place id -> new place id for matched places."""
    min_overlap = cfg["line_min_overlap"]
    max_component = cfg["line_max_component"]
    result: list[LineMatch] = []

    # Equal names, after removing the configured parts (for example a line code prefix "A2").
    strip = _patterns(cfg, "line_name_strip_patterns")

    def name_of(key: str) -> str:
        name = key
        for pattern in strip:
            name = pattern.sub("", name)
        return name or key

    by_name: dict[str, tuple[list[str], list[str]]] = {}
    for side, lines in ((0, old), (1, new)):
        for key in sorted(lines):
            by_name.setdefault(name_of(key), ([], []))[side].append(key)
    named_old: set[str] = set()
    named_new: set[str] = set()
    for _, (olds, news) in sorted(by_name.items()):
        if not olds or not news:
            continue
        relation = "same" if olds == news and len(olds) == 1 else _shape(olds, news)
        result.append(LineMatch(tuple(olds), tuple(news), relation, "same_line_name", 1.0))
        named_old.update(olds)
        named_new.update(news)
    rest_old = sorted(set(old) - named_old)
    rest_new = sorted(set(new) - named_new)

    translated = {k: frozenset(place_map[p] for p in old[k].places if p in place_map) for k in rest_old}
    by_place: dict[str, list[str]] = {}
    for n in rest_new:
        for p in new[n].places:
            by_place.setdefault(p, []).append(n)
    edges: dict[tuple[str, str], float] = {}
    for o in rest_old:
        candidates = sorted({n for p in translated[o] for n in by_place.get(p, [])})
        for n in candidates:
            j = _overlap(translated[o], new[n].places)
            if j >= min_overlap:
                edges[(o, n)] = round(j, 3)

    # Connected components of the bipartite graph of edges.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for o, n in edges:
        a, b = find("o:" + o), find("n:" + n)
        if a != b:
            parent[max(a, b)] = min(a, b)
    components: dict[str, tuple[set[str], set[str]]] = {}
    for o, n in edges:
        root = find("o:" + o)
        olds, news = components.setdefault(root, (set(), set()))
        olds.add(o)
        news.add(n)

    matched_old: set[str] = set()
    matched_new: set[str] = set()
    for olds, news in sorted(components.values(), key=lambda c: (sorted(c[0]), sorted(c[1]))):
        if len(olds) + len(news) > max_component:
            continue  # too entangled to interpret; members stay unmatched
        relation = _shape(olds, news)
        confidence = min(edges[(o, n)] for o in olds for n in news if (o, n) in edges)
        result.append(LineMatch(tuple(sorted(olds)), tuple(sorted(news)), relation, "served_places", confidence))
        matched_old |= olds
        matched_new |= news

    for o in rest_old:
        if o not in matched_old:
            result.append(LineMatch((o,), (), "discontinued", None, None))
    for n in rest_new:
        if n not in matched_new:
            result.append(LineMatch((), (n,), "added", None, None))
    return result
