"""Classification of publication pairs (data-model §12).

A pair of neighbouring publications is

- EQUIVALENT: same analysis summary and content signature (§4.2); no report is built;
- MEANINGFUL_SERVICE_CHANGE: its semantic report has at least one passenger-facing change;
- TECHNICAL_OR_METADATA_ONLY: its report has changes, none of them passenger-facing;
- UNKNOWN: no report says what changed; `reason_type` and `reason` say why.

Which report changes are passenger-facing is set in classification.json. The rules are applied to
stored reports when the site is built, so changing them needs no new reports. Whatever the file
does not list counts as passenger-facing, so a change the rules do not know is never hidden as
technical; unclassified report rows count as passenger-facing too.
"""

from __future__ import annotations

import json
from pathlib import Path

from gtfs_jp_semantic.accounting import CORE_COLUMNS

RULES_PATH = Path(__file__).with_name("classification.json")

MEANINGFUL = "MEANINGFUL_SERVICE_CHANGE"
TECHNICAL = "TECHNICAL_OR_METADATA_ONLY"
EQUIVALENT = "EQUIVALENT"
UNKNOWN = "UNKNOWN"


def load_rules(path: Path = RULES_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _column_class(rules: dict, file: str, column: str | None) -> str:
    cols = rules["columns"].get(file, {})
    return "technical" if column in cols.get("technical", ()) else "passenger_facing"


def classify_report(report: dict, rules: dict) -> dict:
    """{"class": MEANINGFUL or TECHNICAL, "passenger_facing": [reason, ...], "technical": [...]}.

    Reasons name what changed: summary items ("lines.changed", "first_last_changed"), topics
    ("fares"), topic kinds ("calendar_exceptions.service_dates_changed") and non-core columns
    ("trips.txt:trip_headsign")."""
    pf: set[str] = set()
    tech: set[str] = set()
    s = report["summary"]
    for group in ("lines", "places"):
        for k, n in s[group].items():
            if n and k != "unchanged":  # every change of lines and places is passenger-facing
                pf.add(f"{group}.{k}")
    for k in rules["summary"]["counts"]:
        if s.get(k):
            pf.add(k)
    if any(v["before"] != v["after"] for v in s["trips_by_day_type"].values()):
        pf.add("trips_by_day_type")
    if s["fares"]["changed"]:
        pf.add("fares")

    topics = rules["topics"]
    for block in report["other"]:
        topic = block["topic"]
        if topic in topics["technical"]:
            tech.add(topic)
        elif topic in topics["by_kind"]:
            kinds = rules["kinds"][topic]
            for d in block["details"]:
                (tech if d["kind"] in kinds["technical"] else pf).add(f"{topic}.{d['kind']}")
        elif topic in topics["by_column"]:
            pass  # columns come from the accounting below, which is complete (details are capped)
        else:  # listed as passenger-facing, or unknown to the rules
            pf.add(topic)

    for file, acc in report["accounting"]["files"].items():
        if file not in rules["columns"]:
            continue
        core = CORE_COLUMNS.get(file, frozenset())
        changed = set(acc["columns"]) | {x["column"] for x in acc["structure"]
                                           if x["kind"] in ("column_added", "column_removed") and x["column"]}
        for column in sorted(changed - core):
            (tech if _column_class(rules, file, column) == "technical" else pf).add(f"{file}:{column}")
    if report["accounting"]["unclassified"]:
        pf.add("unclassified")

    return {"class": MEANINGFUL if pf else TECHNICAL, "passenger_facing": sorted(pf), "technical": sorted(tech)}
