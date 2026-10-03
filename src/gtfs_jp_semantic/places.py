"""Places (stops) and their matching between two publications (docs/semantic/02-matching.md §3).

A place is a parent station with its platforms, or a stop without a parent. With
`stop_group_radius_m`, stops without a parent that share a normalised name and lie that close
form one place too (the two sides of a street published as separate stops). Matching runs in
three passes, each one-to-one and deterministic: same stop_id, same normalised name nearby,
similar name close by. Nothing is guessed without coordinates except by id and name.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .reader import Table


@dataclass(frozen=True)
class Place:
    place_id: str  # parent stop_id, or the stop_id of a stop without parent
    name: str
    lat: float | None
    lon: float | None
    members: tuple[str, ...]  # every stop_id that belongs to this place (the place itself included)


@dataclass(frozen=True)
class PlaceMatch:
    old: Place | None
    new: Place | None
    method: str | None  # same_id | same_name | near_similar_name
    confidence: float | None
    distance_m: int | None
    status: str  # unchanged | renamed | moved | renamed_moved | added | removed


def _float(value: str) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def build_places(stops: Table | None, cfg: dict | None = None) -> dict[str, Place]:
    """Places keyed by place_id. Entrances, generic nodes and boarding areas (location_type 2-4) are not places."""
    if stops is None or stops.status != "ok":
        return {}
    rows = [dict(zip(stops.header, r)) for r in stops.rows]
    by_id = {r.get("stop_id", ""): r for r in rows if r.get("stop_id")}
    children: dict[str, list[str]] = {}
    places: dict[str, Place] = {}
    for sid, r in sorted(by_id.items()):
        kind = (r.get("location_type") or "0").strip() or "0"
        parent = (r.get("parent_station") or "").strip()
        if kind in ("2", "3", "4"):
            continue
        if kind == "0" and parent and parent in by_id:
            children.setdefault(parent, []).append(sid)
    for sid, r in sorted(by_id.items()):
        kind = (r.get("location_type") or "0").strip() or "0"
        parent = (r.get("parent_station") or "").strip()
        if kind == "1" or (kind == "0" and not (parent and parent in by_id)):
            members = tuple(sorted([sid] + children.get(sid, [])))
            lat, lon = _float(r.get("stop_lat", "")), _float(r.get("stop_lon", ""))
            if (lat is None or lon is None) and children.get(sid):
                pts = [(_float(by_id[c].get("stop_lat", "")), _float(by_id[c].get("stop_lon", ""))) for c in children[sid]]
                pts = [p for p in pts if None not in p]
                if pts:
                    lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
            places[sid] = Place(sid, r.get("stop_name", ""), lat, lon, members)
    if cfg and cfg.get("stop_group_radius_m"):
        lone = [sid for sid, p in places.items() if p.members == (sid,) and (by_id[sid].get("location_type") or "0").strip() in ("", "0")]
        places = _group_lone_stops(places, lone, cfg)
    return places


def _group_lone_stops(places: dict[str, Place], lone: list[str], cfg: dict) -> dict[str, Place]:
    """Stops without parent and with the same normalised name, each within stop_group_radius_m of
    another, become one place: id the smallest stop_id, name of that stop, position the mean."""
    norm = NameNormaliser(cfg["stop_name_suffixes"], cfg["stop_name_strip_patterns"])
    radius = cfg["stop_group_radius_m"]
    by_name: dict[str, list[str]] = {}
    for sid in sorted(lone):
        name = norm(places[sid].name)
        if name and places[sid].lat is not None and places[sid].lon is not None:
            by_name.setdefault(name, []).append(sid)
    result = dict(places)
    for ids in by_name.values():
        if len(ids) < 2:
            continue
        parent = {i: i for i in ids}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if distance_m(places[a], places[b]) <= radius:
                    parent[max(find(a), find(b))] = min(find(a), find(b))
        groups: dict[str, list[str]] = {}
        for i in ids:
            groups.setdefault(find(i), []).append(i)
        for members in groups.values():
            if len(members) < 2:
                continue
            first = places[members[0]]
            for m in members:
                del result[m]
            result[first.place_id] = Place(first.place_id, first.name,
                                           sum(places[m].lat for m in members) / len(members),
                                           sum(places[m].lon for m in members) / len(members), tuple(members))
    return result


def place_of_stop(places: dict[str, Place]) -> dict[str, str]:
    """stop_id -> place_id for every member stop."""
    return {m: p.place_id for p in places.values() for m in p.members}


class NameNormaliser:
    def __init__(self, suffixes: list[str], patterns: list[str]) -> None:
        self._suffixes = sorted({self._basic(s) for s in suffixes if s}, key=len, reverse=True)
        self._patterns = [re.compile(p) for p in patterns]

    @staticmethod
    def _basic(text: str) -> str:
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))

    def __call__(self, name: str) -> str:
        text = self._basic(name)
        changed = True
        while changed and text:
            changed = False
            for s in self._suffixes:
                if text.endswith(s) and len(text) > len(s):
                    text, changed = text[: -len(s)], True
            for p in self._patterns:
                stripped = p.sub("", text)
                if stripped != text and stripped:
                    text, changed = stripped, True
        return text


def distance_m(a: Place, b: Place) -> float | None:
    if None in (a.lat, a.lon, b.lat, b.lon):
        return None
    r = 6371008.8
    p1, p2 = math.radians(a.lat), math.radians(b.lat)
    dp, dl = p2 - p1, math.radians(b.lon - a.lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


GRID_DEG = 0.01  # about 1.1 km north-south; every search radius must stay below one cell


def _grid(places: dict[str, Place], ids: list[str]) -> dict[tuple[int, int], list[str]]:
    cells: dict[tuple[int, int], list[str]] = {}
    for k in ids:
        p = places[k]
        if p.lat is not None and p.lon is not None:
            cells.setdefault((math.floor(p.lat / GRID_DEG), math.floor(p.lon / GRID_DEG)), []).append(k)
    return cells


def _nearby(cells: dict[tuple[int, int], list[str]], p: Place) -> list[str]:
    if p.lat is None or p.lon is None:
        return []
    ci, cj = math.floor(p.lat / GRID_DEG), math.floor(p.lon / GRID_DEG)
    return sorted(k for di in (-1, 0, 1) for dj in (-1, 0, 1) for k in cells.get((ci + di, cj + dj), []))


def match_places(old: dict[str, Place], new: dict[str, Place], cfg: dict) -> list[PlaceMatch]:
    norm = NameNormaliser(cfg["stop_name_suffixes"], cfg["stop_name_strip_patterns"])
    same_id_max = cfg["stop_same_id_max_m"]
    name_radius = cfg["stop_name_radius_m"]
    near_radius = cfg["stop_near_radius_m"]
    min_sim = cfg["stop_name_min_similarity"]
    moved_min = cfg["stop_moved_min_m"]
    accept = cfg["accept_confidence"]

    names_old = {k: norm(p.name) for k, p in old.items()}
    names_new = {k: norm(p.name) for k, p in new.items()}
    pairs: dict[str, tuple[str, str, float]] = {}  # old id -> (new id, method, confidence)
    used_new: set[str] = set()

    def take(candidates: list[tuple[float, str, str, str, float]]) -> None:
        # candidates: (sort key, old id, new id, method, confidence); lowest key first, ids break ties.
        for _, o, n, method, conf in sorted(candidates):
            if o in pairs or n in used_new or conf < accept:
                continue
            pairs[o] = (n, method, conf)
            used_new.add(n)

    # 1. same id
    first = []
    for pid in sorted(set(old) & set(new)):
        d = distance_m(old[pid], new[pid])
        same_name = names_old[pid] == names_new[pid]
        near = d is not None and d <= same_id_max
        if same_name and (near or d is None):
            conf = 1.0
        elif same_name or near:
            conf = 0.9
        else:
            continue  # the id now names another place; left for the other passes
        first.append((0.0, pid, pid, "same_id", conf))
    take(first)

    rest_old = [k for k in sorted(old) if k not in pairs]
    rest_new = [k for k in sorted(new) if k not in used_new]
    if max(name_radius, near_radius) >= 1000:
        raise ValueError("stop matching radii must stay below 1000 m (grid cell size)")
    cells = _grid(new, rest_new)

    # 2. same normalised name nearby, nearest first
    second = []
    for o in rest_old:
        for n in _nearby(cells, old[o]):
            if names_old[o] and names_old[o] == names_new[n]:
                d = distance_m(old[o], new[n])
                if d is not None and d <= name_radius:
                    second.append((d, o, n, "same_name", round(1.0 - 0.1 * d / name_radius, 3)))
    take(second)

    # 3. close by with a similar name, best score first
    third = []
    for o in (k for k in rest_old if k not in pairs):
        for n in (k for k in _nearby(cells, old[o]) if k not in used_new):
            d = distance_m(old[o], new[n])
            if d is None or d > near_radius:
                continue
            sim = SequenceMatcher(None, names_old[o], names_new[n]).ratio()
            if sim >= min_sim:
                score = round(sim * (1.0 - 0.5 * d / near_radius), 3)
                third.append((-score, o, n, "near_similar_name", score))
    take(third)

    result: list[PlaceMatch] = []
    for o in sorted(old):
        if o in pairs:
            n, method, conf = pairs[o]
            d = distance_m(old[o], new[n])
            renamed = names_old[o] != names_new[n]
            moved = d is not None and d >= moved_min
            status = "renamed_moved" if renamed and moved else "renamed" if renamed else "moved" if moved else "unchanged"
            result.append(PlaceMatch(old[o], new[n], method, conf, None if d is None else round(d), status))
        else:
            result.append(PlaceMatch(old[o], None, None, None, None, "removed"))
    for n in sorted(new):
        if n not in used_new:
            result.append(PlaceMatch(None, new[n], None, None, None, "added"))
    return result
