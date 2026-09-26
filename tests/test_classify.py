import copy
import json
import re
import unittest
from pathlib import Path

from gtfs_jp_monitor.classify import MEANINGFUL, TECHNICAL, classify_report, load_rules

from .schema_support import FIXTURES, load_json

ROOT = Path(__file__).resolve().parent.parent
RULES = load_rules()


def quiet(report: dict) -> dict:
    """The example report with every change removed."""
    r = copy.deepcopy(report)
    s = r["summary"]
    for group in ("lines", "places"):
        s[group] = {k: 0 for k in s[group]}
    for k in ("first_last_changed", "trip_moves", "date_changes"):
        s[k] = 0
    s["trips_by_day_type"] = {d: {"before": 5, "after": 5} for d in s["trips_by_day_type"]}
    s["fares"] = dict(s["fares"], changed=False)
    r["other"] = []
    r["accounting"]["unclassified"] = []
    for acc in r["accounting"]["files"].values():
        acc["columns"], acc["structure"] = {}, []
    return r


def block(topic: str, *kinds: str) -> dict:
    return {"topic": topic, "counts": {}, "evidence": [], "truncated": 0,
            "details": [{"kind": k, "file": None, "column": None, "id": None, "key": None, "old": None, "new": None,
                         "counts": {}} for k in kinds]}


class RulesCoverTheReportTest(unittest.TestCase):
    """Every topic, summary item and kind the engine can emit has a place in the rules."""

    def test_topics_of_the_schema(self):
        schema = json.loads((ROOT / "schemas" / "semantic-report.schema.json").read_text(encoding="utf-8"))
        enum = None
        stack = [schema]
        while stack and enum is None:
            node = stack.pop()
            if isinstance(node, dict):
                if isinstance(node.get("topic"), dict) and "enum" in node["topic"]:
                    enum = set(node["topic"]["enum"])
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        listed = {t for group in RULES["topics"].values() for t in group}
        self.assertEqual(enum - listed, set())

    def test_summary_items(self):
        schema = json.loads((ROOT / "schemas" / "semantic-report.schema.json").read_text(encoding="utf-8"))
        summary = schema["properties"]["summary"]["properties"]
        for group in ("lines", "places"):
            self.assertEqual(set(summary[group]["required"]) - {"unchanged"}, set(RULES["summary"][group]))

    def test_calendar_kinds_of_the_engine(self):
        source = (ROOT / "src" / "gtfs_jp_semantic" / "report.py").read_text(encoding="utf-8")
        emitted = set(re.findall(r'other\["(calendar_[a-z_]+)"\]', source)) | {"service_dates_changed"}
        kinds = RULES["kinds"]["calendar_exceptions"]
        self.assertEqual(emitted - set(kinds["passenger_facing"]) - set(kinds["technical"]), set())


class ClassifyTest(unittest.TestCase):
    def setUp(self):
        self.base = quiet(load_json(FIXTURES / "semantic" / "report-example.json"))

    def test_no_change_and_technical_topics(self):
        self.assertEqual(classify_report(self.base, RULES)["class"], TECHNICAL)
        r = copy.deepcopy(self.base)
        r["other"] = [block("feed_info", "field_changed"), block("calendar_exceptions", "calendar_outside_shared")]
        out = classify_report(r, RULES)
        self.assertEqual((out["class"], out["technical"]),
                         (TECHNICAL, ["calendar_exceptions.calendar_outside_shared", "feed_info"]))

    def test_service_changes(self):
        for change in (lambda s: s["lines"].update(changed=1), lambda s: s["places"].update(moved=2),
                       lambda s: s.update(first_last_changed=1), lambda s: s["fares"].update(changed=True)):
            r = copy.deepcopy(self.base)
            change(r["summary"])
            self.assertEqual(classify_report(r, RULES)["class"], MEANINGFUL)
        r = copy.deepcopy(self.base)
        r["other"] = [block("calendar_exceptions", "service_dates_changed")]
        self.assertEqual(classify_report(r, RULES)["passenger_facing"], ["calendar_exceptions.service_dates_changed"])

    def test_columns(self):
        r = copy.deepcopy(self.base)
        acc = r["accounting"]["files"].setdefault("stop_times.txt", copy.deepcopy(next(iter(r["accounting"]["files"].values()))))
        acc["columns"] = {"timepoint": 3, "arrival_time": 9}  # arrival_time is core: the summary speaks for it
        out = classify_report(r, RULES)
        self.assertEqual((out["class"], out["technical"]), (TECHNICAL, ["stop_times.txt:timepoint"]))
        acc["structure"] = [{"kind": "column_added", "column": "pickup_type", "bucket": "classified"}]
        self.assertEqual(classify_report(r, RULES)["passenger_facing"], ["stop_times.txt:pickup_type"])
        acc["columns"] = {"new_column_nobody_listed": 1}  # unknown columns count as passenger-facing
        self.assertEqual(classify_report(r, RULES)["class"], MEANINGFUL)

    def test_unknown_topic_and_unclassified_rows(self):
        r = copy.deepcopy(self.base)
        r["other"] = [block("some_future_topic", "x")]
        self.assertEqual(classify_report(r, RULES)["passenger_facing"], ["some_future_topic"])
        r = copy.deepcopy(self.base)
        r["accounting"]["unclassified"] = ["c1"]
        self.assertEqual(classify_report(r, RULES)["passenger_facing"], ["unclassified"])


if __name__ == "__main__":
    unittest.main()
