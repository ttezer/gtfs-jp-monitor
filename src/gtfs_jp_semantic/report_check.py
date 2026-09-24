"""Referential checks for gtfs-jp-semantic-report/1 that JSON Schema cannot express.

The engine runs these before writing a report; an empty list means the report is consistent.
"""

from __future__ import annotations


def check_report(doc: dict) -> list[str]:
    problems: list[str] = []
    places = doc.get("places", [])
    n_places = len(places)
    keys = [line["key"] for line in doc.get("lines", [])]
    key_set = set(keys)
    if len(key_set) != len(keys):
        problems.append("duplicate line keys")

    def place_ok(ref: int, where: str) -> None:
        if not 0 <= ref < n_places:
            problems.append(f"{where}: place index {ref} out of range")

    for i, place in enumerate(places):
        for ref in place["lines"]:
            if ref not in key_set:
                problems.append(f"places[{i}]: unknown line {ref!r}")
        status = place["status"]
        if status == "added" and (place["old"] is not None or place["new"] is None):
            problems.append(f"places[{i}]: added place must have only a new side")
        if status == "removed" and (place["new"] is not None or place["old"] is None):
            problems.append(f"places[{i}]: removed place must have only an old side")
        if status not in ("added", "removed") and (place["old"] is None or place["new"] is None):
            problems.append(f"places[{i}]: {status} place needs both sides")
        if status in ("moved", "renamed_moved") and place["moved_m"] is None:
            problems.append(f"places[{i}]: moved place needs moved_m")

    for line in doc.get("lines", []):
        name = line["key"]
        status = line["status"]
        if status == "added" and line["old"] is not None:
            problems.append(f"line {name}: added line has an old side")
        if status == "discontinued" and line["new"] is not None:
            problems.append(f"line {name}: discontinued line has a new side")
        for ref in line["related"]:
            if ref not in key_set:
                problems.append(f"line {name}: unknown related line {ref!r}")
        for p, pattern in enumerate(line["patterns"]):
            for e, edit in enumerate(pattern["edits"]):
                for ref in edit["places"]:
                    place_ok(ref, f"line {name} patterns[{p}].edits[{e}]")
        for t, table in enumerate(line["timetables"]):
            where = f"line {name} timetables[{t}]"
            sizes = {}
            for side in ("old", "new"):
                tt = table[side]
                if tt is None:
                    sizes[side] = 0
                    continue
                for ref in tt["places"]:
                    place_ok(ref, f"{where}.{side}")
                for k, trip in enumerate(tt["trips"]):
                    if len(trip["times"]) != len(tt["places"]):
                        problems.append(f"{where}.{side}.trips[{k}]: {len(trip['times'])} times for {len(tt['places'])} places")
                sizes[side] = len(tt["trips"])
            seen = {"old": set(), "new": set()}
            for k, (o, n) in enumerate(table["pairs"]):
                if o is None and n is None:
                    problems.append(f"{where}.pairs[{k}]: both sides null")
                for side, idx in (("old", o), ("new", n)):
                    if idx is None:
                        continue
                    if idx >= sizes[side]:
                        problems.append(f"{where}.pairs[{k}]: {side} index {idx} out of range")
                    elif idx in seen[side]:
                        problems.append(f"{where}.pairs[{k}]: {side} trip {idx} paired twice")
                    seen[side].add(idx)
            for side in ("old", "new"):
                if len(seen[side]) != sizes[side]:
                    problems.append(f"{where}: every {side} trip must appear in pairs exactly once")

    tables = {(line["key"], t["direction"], t["day_type"]): t for line in doc.get("lines", []) for t in line["timetables"]}
    used: set[tuple] = set()
    for k, move in enumerate(doc.get("moves", [])):
        where = f"moves[{k}]"
        for side in ("old", "new"):
            ref = move[side]
            table = tables.get((ref["line"], ref["direction"], move["day_type"]))
            if table is None or table[side] is None or ref["trip"] >= len(table[side]["trips"]):
                problems.append(f"{where}.{side}: no such timetable trip")
                continue
            # A moved trip is unpaired in its own line.
            partner = [p for p in table["pairs"] if p[0 if side == "old" else 1] == ref["trip"]]
            if not partner or partner[0][1 if side == "old" else 0] is not None:
                problems.append(f"{where}.{side}: trip is paired in its own line")
            key = (side, ref["line"], ref["direction"], move["day_type"], ref["trip"])
            if key in used:
                problems.append(f"{where}.{side}: trip moved twice")
            used.add(key)
        for e, edit in enumerate(move["edits"]):
            for ref in edit["places"]:
                place_ok(ref, f"{where}.edits[{e}]")

    for name, f in doc.get("accounting", {}).get("files", {}).items():
        rows = f["added"] + f["removed"] + f["changed_rows"] + len(f["structure"])
        buckets = f["classified"] + f["outside_comparison"] + f["unclassified"]
        if not rows == buckets == f["changes"]:
            problems.append(f"accounting.files[{name}]: rows {rows}, buckets {buckets}, changes {f['changes']}")
    coverage = doc.get("header", {}).get("coverage")
    if coverage:
        parts = coverage["explained"] + coverage["outside_comparison"] + coverage["unclassified"]
        if parts != coverage["raw_total"]:
            problems.append(f"coverage parts sum to {parts}, raw_total is {coverage['raw_total']}")
    return problems
