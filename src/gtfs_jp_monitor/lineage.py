"""Build a feed-local route lineage from consecutive semantic reports.

The semantic report is the source of matching evidence.  This module only carries high-confidence
one-to-one ``same`` links forward; split/merge and ambiguous many-to-many mappings remain explicit
relations instead of being silently collapsed into one identity.
"""

from __future__ import annotations

import hashlib


SCHEMA = "gtfs-route-lineage/1"
DEFAULT_MIN_CONFIDENCE = 0.8


def _node(uid: str, route_id: str) -> tuple[str, str]:
    return uid, route_id


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def add(self, item: tuple[str, str]) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: tuple[str, str]) -> tuple[str, str]:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            nxt = self.parent[item]
            self.parent[item] = root
            item = nxt
        return root

    def union(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        self.add(a)
        self.add(b)
        a_root, b_root = self.find(a), self.find(b)
        if a_root != b_root:
            self.parent[max(a_root, b_root)] = min(a_root, b_root)


def _route_ids(report: dict, side: str) -> list[str]:
    ids: set[str] = set()
    for line in report.get("lines", ()):
        entry = line.get(side) or {}
        ids.update(entry.get("route_ids", ()) if isinstance(entry, dict) else entry)
    return sorted(ids)


def build_route_lineage(reports: list[dict], *, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> dict:
    """Return canonical route occurrences and explicit old/new relations.

    ``reports`` must be in publication order for one feed.  A route is linked across a pair when
    its source id is unchanged, or when the report supplies one high-confidence one-to-one
    ``same`` mapping.  The canonical id is derived from the first occurrence in the supplied
    history, so rebuilding the same ordered history is deterministic.
    """
    if not 0 <= min_confidence <= 1:
        raise ValueError("min_confidence must be between 0 and 1")
    if not reports:
        return {"schema": SCHEMA, "feed": None, "routes": [], "relations": []}

    feed = reports[0].get("header", {}).get("feed")
    uf = _UnionFind()
    occurrences: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    relations: list[dict] = []

    for report in reports:
        header = report.get("header", {})
        old_uid = (header.get("old") or {}).get("uid")
        new_uid = (header.get("new") or {}).get("uid")
        if not old_uid or not new_uid:
            raise ValueError("each report needs header.old.uid and header.new.uid")
        old_ids, new_ids = _route_ids(report, "old"), _route_ids(report, "new")
        for uid, ids in ((old_uid, old_ids), (new_uid, new_ids)):
            for route_id in ids:
                item = _node(uid, route_id)
                uf.add(item)
                if item not in seen:
                    seen.add(item)
                    occurrences.append(item)

        for route_id in set(old_ids) & set(new_ids):
            uf.union(_node(old_uid, route_id), _node(new_uid, route_id))

        for mapping in (report.get("mappings") or {}).get("routes", ()):
            old_routes, new_routes = list(mapping.get("old", ())), list(mapping.get("new", ()))
            relation = mapping.get("relation", "same")
            confidence = mapping.get("confidence")
            relations.append({
                "old": [{"publication_uid": old_uid, "route_id": r} for r in old_routes],
                "new": [{"publication_uid": new_uid, "route_id": r} for r in new_routes],
                "relation": relation,
                "confidence": confidence,
            })
            if (relation == "same" and confidence is not None and confidence >= min_confidence
                    and len(old_routes) == len(new_routes) == 1):
                uf.union(_node(old_uid, old_routes[0]), _node(new_uid, new_routes[0]))

    members: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for item in occurrences:
        members.setdefault(uf.find(item), []).append(item)
    canonical: dict[tuple[str, str], str] = {}
    for group in members.values():
        seed_uid, seed_route = min(group)
        seed = f"{feed.get('org_id', '')}/{feed.get('feed_id', '')}|{seed_uid}|{seed_route}"
        ident = "route-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
        for item in group:
            canonical[item] = ident

    return {
        "schema": SCHEMA,
        "feed": feed,
        "routes": [{"publication_uid": uid, "source_route_id": route_id,
                     "canonical_route_id": canonical[(uid, route_id)]}
                    for uid, route_id in occurrences],
        "relations": relations,
    }
