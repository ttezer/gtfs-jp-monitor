import copy
import unittest

from gtfs_jp_monitor.classify import load_rules
from gtfs_jp_monitor.impact import assess, event_class

from .test_semantic_events import build
from .test_semantic_report import NEW, OLD

RULES = load_rules()


class ImpactTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report, _ = build(OLD, NEW)
        cls.impact = assess(cls.report, RULES)

    def classes(self, kind):
        return self.impact["types"].get(kind, {})

    def test_no_events_no_impact(self):
        report, _ = build(OLD, NEW, events=False)
        self.assertIsNone(assess(report, RULES))

    def test_event_classes(self):
        self.assertEqual(self.classes("ROUTE_ADDED"), {"major": 1})
        self.assertEqual(self.classes("FARE_CHANGED"), {"major": 1})  # F1: 200 -> 220 yen
        self.assertEqual(self.classes("STOP_ADDED"), {"major": 1})  # 新団地 is served by line 2
        self.assertEqual(self.classes("SERVICE_INCREASED"), {"minor": 1})  # one trip: below the two-trip minimum
        self.assertEqual(self.classes("STOP_RENAMED"), {"minor": 1})
        self.assertEqual(self.classes("TRIPS_RETIMED"), {"minor": 1})
        self.assertEqual(self.classes("OTHER_CHANGED"), {"minor": 1})  # a holiday date no longer served
        self.assertEqual(self.impact["class"], "major")

    def test_lines_take_their_highest_class(self):
        lines = self.impact["lines"]
        self.assertEqual(lines["3"]["class"], "major")  # added
        self.assertEqual(lines["2"]["class"], "major")  # its only trip skips 旧団地 for 新団地
        self.assertEqual(sorted(lines["2"]["types"]), ["STOP_INSERTED", "STOP_SKIPPED"])  # no stop events of its own
        self.assertEqual(lines["1"]["class"], "minor")  # retimed, a trip moved away

    def test_counts(self):
        (day_type,) = self.impact["trips"]
        self.assertEqual(self.impact["trips"][day_type], {"added": 1, "removed": 0, "retimed": 1})
        self.assertEqual(self.impact["service_lines"][day_type], {"increased": 1, "reduced": 0})

    def test_thresholds(self):
        service = {"type": "SERVICE_REDUCED", "line": "1", "day_type": "mon", "before": 40, "after": 37, "evidence": []}
        self.assertEqual(event_class(service, self.report, RULES, set(), {}), "minor")  # 3 of 40 < 10%
        self.assertEqual(event_class(dict(service, after=35), self.report, RULES, set(), {}), "major")
        pattern = {"type": "DETOUR_ADDED", "line": "1", "direction": "0", "places": [0], "count": 2, "evidence": []}
        self.assertEqual(event_class(pattern, self.report, RULES, set(), {("1", "0"): 5}), "minor")
        self.assertEqual(event_class(dict(pattern, count=3), self.report, RULES, set(), {("1", "0"): 5}), "major")
        self.assertEqual(event_class(dict(pattern, count=None), self.report, RULES, set(), {}), "major")  # dominant pattern

    def test_a_meaningful_pair_is_at_least_minor(self):
        report = copy.deepcopy(self.report)
        report["events"] = [{"type": "OTHER_CHANGED", "topic": "feed_info", "evidence": []}]
        self.assertEqual(assess(report, RULES)["class"], "minor")  # the report's other changes are passenger-facing

    def test_fares_only_rewritten_are_minor(self):
        fare = {"type": "FARE_CHANGED", "evidence": []}
        report = copy.deepcopy(self.report)
        report["summary"]["fares"] = {"changed": True, "classes_added": 1, "classes_removed": 1, "prices_changed": 0}
        self.assertEqual(event_class(fare, report, RULES, set(), {}), "minor")  # a fare class renamed
        report["summary"]["fares"]["classes_added"] = 2
        self.assertEqual(event_class(fare, report, RULES, set(), {}), "major")  # a new fare class

    def test_technical(self):
        report = copy.deepcopy(self.report)
        # a stop no line serves, and a technical topic
        report["places"][0]["lines"] = []
        report["events"] = [{"type": "STOP_ADDED", "place": 0, "evidence": []},
                            {"type": "OTHER_CHANGED", "topic": "feed_info", "evidence": []}]
        report["summary"]["lines"] = dict.fromkeys(report["summary"]["lines"], 0)
        report["summary"]["places"] = dict.fromkeys(report["summary"]["places"], 0)
        report["summary"].update(first_last_changed=0, trip_moves=0, date_changes=0)
        report["summary"]["trips_by_day_type"] = {k: {"before": 1, "after": 1} for k in report["summary"]["trips_by_day_type"]}
        report["summary"]["fares"] = {"changed": False, "classes_added": 0, "classes_removed": 0, "prices_changed": 0}
        report["other"] = [o for o in report["other"] if o["topic"] not in ("fares", "calendar_exceptions")]
        report["accounting"]["unclassified"] = []
        report["accounting"]["files"] = {}
        impact = assess(report, RULES)
        self.assertEqual((impact["class"], impact["types"]), ("technical", {"OTHER_CHANGED": {"technical": 1},
                                                                            "STOP_ADDED": {"technical": 1}}))


if __name__ == "__main__":
    unittest.main()
