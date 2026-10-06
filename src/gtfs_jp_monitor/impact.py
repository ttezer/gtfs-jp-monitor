"""Impact of the changes a semantic report found (data-model §12.1).

Every event of a report (docs/semantic/03-report.md, Events) is major, minor or technical; a line
takes the highest class of its own events (a stop change reaches a line through its pattern
events), the report the highest of all. Like the pair classes (§12), the rules are
in classification.json and are applied when the site is built, so changing them needs no new
reports. A report without events (built before engine 0.7.0 or without report.events) has no
impact. Technical are exactly the changes the pair classification counts as technical; the
passenger-facing ones are major or minor.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .classify import MEANINGFUL, classify_report

CLASSES = ("major", "minor", "technical")
_RANK = {c: i for i, c in enumerate(reversed(CLASSES))}


def _highest(classes) -> str | None:
    return max(classes, key=_RANK.get, default=None)


def _direction_trips(report: dict) -> dict[tuple[str, str], int]:
    """Trips of each line direction over the compared day types, the larger side."""
    out: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for line in report["lines"]:
        for entry in line["trips"]:
            side = out[(line["key"], entry["direction"])]
            for band in entry["bands"].values():
                side[0] += band["before"]
                side[1] += band["after"]
    return {k: max(v) for k, v in out.items()}


def _other_class(report: dict, topic: str, rules: dict, reasons: set[str]) -> str:
    """Technical when the pair classification places this topic's changes as technical."""
    topics = rules["topics"]
    if topic in topics["technical"]:
        return "technical"
    if topic in topics["by_kind"]:
        return "minor" if any(r.startswith(topic + ".") for r in reasons) else "technical"
    if topic in topics["by_column"]:
        return "minor" if any(":" in r for r in reasons) else "technical"
    return "minor"


def event_class(event: dict, report: dict, rules: dict, reasons: set[str], direction_trips: dict) -> str:
    imp = rules["impact"]
    kind = event["type"]
    if kind in imp["major"]:
        return "major"
    if kind in imp["major_if_served"]:
        place = report["places"][event["place"]] if event.get("place") is not None else None
        return "major" if place and place["lines"] else "technical"
    if kind in imp["major_if_service_share"]:
        before, after = event["before"], event["after"]
        change = abs(after - before)
        big = change >= imp["service_major_min_trips"] and (before == 0 or change >= imp["service_major_share"] * before)
        return "major" if big else "minor"
    if kind in imp["major_if_pattern_share"]:
        if event.get("count") is None:  # the dominant pattern changed: most trips run it
            return "major"
        total = direction_trips.get((event["line"], event["direction"]), 0)
        return "major" if total and event["count"] >= imp["pattern_major_share"] * total else "minor"
    if kind in imp["major_if_fare_price"]:
        # Prices, or how many fare classes there are. Fare rules rewritten and fare classes only
        # renamed (one added, one removed) change no price a passenger pays.
        f = report["summary"]["fares"]
        return "major" if f["prices_changed"] or f["classes_added"] != f["classes_removed"] else "minor"
    if kind == "OTHER_CHANGED":
        return _other_class(report, event["topic"], rules, reasons)
    return "minor"


def assess(report: dict, rules: dict) -> dict | None:
    """{"class", "lines": {key: {"class", "types": {event type: n}}}, "types": {type: {class: n}},
    "trips": {day type: {"added", "removed", "retimed"}}, "service_lines": {day type: {"increased",
    "reduced"}}} for a report with events, else None. Language-neutral: the page words it."""
    events = report.get("events")
    if events is None:
        return None
    pair = classify_report(report, rules)
    reasons = set(pair["passenger_facing"])
    totals = _direction_trips(report)
    classes = [event_class(e, report, rules, reasons, totals) for e in events]
    lines: dict[str, dict] = defaultdict(lambda: {"classes": [], "types": Counter()})
    types: dict[str, Counter] = defaultdict(Counter)
    trips: dict[str, Counter] = defaultdict(Counter)
    service: dict[str, Counter] = defaultdict(Counter)
    for e, c in zip(events, classes):
        types[e["type"]][c] += 1
        if e.get("line"):
            lines[e["line"]]["classes"].append(c)
            lines[e["line"]]["types"][e["type"]] += 1
        if e["type"] in ("SERVICE_INCREASED", "SERVICE_REDUCED"):
            delta = e["after"] - e["before"]
            trips[e["day_type"]]["added" if delta > 0 else "removed"] += abs(delta)
            service[e["day_type"]]["increased" if delta > 0 else "reduced"] += 1
        elif e["type"] == "TRIPS_RETIMED":
            trips[e["day_type"]]["retimed"] += e["count"]
    # Technical is exactly what the pair classification calls technical: a passenger-facing change
    # no event type covers (a date-specific difference, an unclassified row) makes it at least minor.
    floor = ["minor"] if pair["class"] == MEANINGFUL else []
    return {
        "class": _highest(classes + floor),
        "lines": {k: {"class": _highest(v["classes"]), "types": dict(sorted(v["types"].items()))} for k, v in sorted(lines.items())},
        "types": {t: dict(sorted(c.items())) for t, c in sorted(types.items())},
        "trips": {dt: {k: c[k] for k in ("added", "removed", "retimed")} for dt, c in sorted(trips.items())},
        "service_lines": {dt: {k: c[k] for k in ("increased", "reduced")} for dt, c in sorted(service.items())},
    }
