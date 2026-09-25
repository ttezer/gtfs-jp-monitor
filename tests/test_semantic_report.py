import tempfile
import unittest
import zipfile
from pathlib import Path

from gtfs_jp_monitor.canonical import dumps
from gtfs_jp_semantic.accounting import Evidence, classify
from gtfs_jp_semantic.report import _attribute_details, _date_changes, _rows_for, build_report
from gtfs_jp_semantic.trips import Trip
from gtfs_jp_semantic.report_check import check_report

from .schema_support import HAVE_JSONSCHEMA, errors, validator

CALENDAR = ("service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "WK,1,1,1,1,1,0,0,20260401,20260630\n")
ROUTES = "route_id,route_short_name,route_long_name,route_type\nR1,1,,3\nR2,2,,3\n"


def stops(rows):
    return "stop_id,stop_name,stop_lat,stop_lon\n" + "".join(f"{i},{n},{la},{lo}\n" for i, n, la, lo in rows)


def stop_times(trips):
    """trips: {trip_id: [(stop_id, 'HH:MM'), ...]}"""
    lines = ["trip_id,arrival_time,departure_time,stop_id,stop_sequence"]
    for tid, calls in trips.items():
        for seq, (sid, hm) in enumerate(calls, 1):
            lines.append(f"{tid},{hm}:00,{hm}:00,{sid},{seq}")
    return "\n".join(lines) + "\n"


OLD = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\nA,Bus,https://example.jp,Asia/Tokyo\n",
    "stops.txt": stops([("S1", "駅前", 33.0, 131.0), ("S2", "市役所", 33.01, 131.0), ("S3", "病院", 33.02, 131.0),
                        ("S4", "旧団地", 33.03, 131.0)]),
    "routes.txt": ROUTES,
    "calendar.txt": CALENDAR,
    "calendar_dates.txt": "service_id,date,exception_type\nHOL,20260515,1\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR1,WK,T1,0\nR1,WK,T2,0\nR2,WK,U1,0\nR1,HOL,X1,0\nR1,WK,T4,0\n",
    "stop_times.txt": stop_times({
        "T1": [("S1", "06:40"), ("S2", "06:45"), ("S3", "06:50")],
        "T2": [("S1", "07:40"), ("S2", "07:45"), ("S3", "07:50")],
        "U1": [("S1", "08:00"), ("S4", "08:10")],
        "X1": [("S1", "12:00"), ("S3", "12:10")],
        "T4": [("S1", "10:00"), ("S2", "10:05")],
    }),
    "fare_attributes.txt": "fare_id,price,currency_type,payment_method,transfers\nF1,200,JPY,0,0\n",
}
NEW = dict(OLD, **{
    "stops.txt": stops([("S1", "駅前", 33.0, 131.0), ("S2", "市役所前", 33.01, 131.0), ("S3", "病院", 33.02, 131.0),
                        ("S5", "新団地", 33.1, 131.0)]),
    "calendar_dates.txt": "service_id,date,exception_type\n",
    "routes.txt": ROUTES + "R3,3,,3\n",
    "trips.txt": "route_id,service_id,trip_id,direction_id\nR1,WK,T1,0\nR1,WK,T2,0\nR1,WK,T3,0\nR2,WK,U1,0\nR3,WK,T4,0\n",
    "stop_times.txt": stop_times({
        "T1": [("S1", "06:45"), ("S2", "06:50"), ("S3", "06:55")],
        "T2": [("S1", "07:40"), ("S2", "07:45"), ("S3", "07:50")],
        "T3": [("S1", "09:00"), ("S2", "09:05"), ("S3", "09:10")],
        "U1": [("S1", "08:00"), ("S5", "08:10")],
        "T4": [("S1", "10:00"), ("S2", "10:05"), ("S3", "10:10")],
    }),
    "fare_attributes.txt": "fare_id,price,currency_type,payment_method,transfers\nF1,220,JPY,0,0\n",
})
PUB = {"from_date": "2026-04-01", "to_date": "2026-06-30", "published_at": None, "memo": ""}


def write_zip(path: Path, files: dict) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.old_zip, cls.new_zip = write_zip(d / "old.zip", OLD), write_zip(d / "new.zip", NEW)
        cls.report, cls.raw = cls.build()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def build(cls):
        return build_report(cls.old_zip, cls.new_zip, feed={"org_id": "sample-city", "feed_id": "SampleBus"},
                            old_pub=dict(PUB, uid="1c8d1613-0633-4b70-9268-c88785f29ac3"),
                            new_pub=dict(PUB, uid="2da131b0-cb13-4bc2-b8eb-d925bf6050ce"), analysis_key="v0.14.0__auto")

    def line(self, key):
        return next(l for l in self.report["lines"] if l["key"] == key)

    def place_name(self, index):
        p = self.report["places"][index]
        return (p["new"] or p["old"])["name"]

    def test_consistent_deterministic_and_valid(self):
        self.assertEqual(check_report(self.report), [])
        self.assertEqual(dumps(self.build()[0]), dumps(self.report))
        if HAVE_JSONSCHEMA:
            self.assertEqual(errors(validator("semantic-report.schema.json"), self.report), [])

    def test_summary(self):
        s = self.report["summary"]
        self.assertEqual(s["lines"]["changed"], 2)
        self.assertEqual(s["places"], {"added": 1, "removed": 1, "renamed": 1, "moved": 0})
        self.assertEqual(s["trips_by_day_type"]["mon,tue,wed,thu,fri,hol"], {"before": 4, "after": 5})
        self.assertEqual(s["lines"]["added"], 1)
        self.assertEqual(s["trip_moves"], 1)
        self.assertEqual(s["fares"], {"changed": True, "classes_added": 0, "classes_removed": 0, "prices_changed": 1})
        self.assertEqual(s["first_last_changed"], 1)  # line 1 first 06:40 -> 06:45 is below 10 min; last 10:00 -> 09:00

    def test_timetable_and_pairs(self):
        line = self.line("1")
        self.assertEqual(line["status"], "changed")
        (table,) = line["timetables"]
        self.assertEqual(table["day_type"], "mon,tue,wed,thu,fri,hol")
        self.assertEqual([self.place_name(i) for i in table["new"]["places"]], ["駅前", "市役所前", "病院"])
        self.assertEqual(table["old"]["places"], table["new"]["places"])  # old side speaks new place ids
        self.assertEqual([t["trip_id"] for t in table["old"]["trips"]], ["T1", "T2", "T4"])
        self.assertEqual(table["new"]["trips"][0]["times"], [405, 410, 415])
        self.assertEqual(table["pairs"], [[0, 0], [1, 1], [None, 2], [2, None]])
        (weekday,) = line["trips"]
        self.assertEqual((weekday["direction"], weekday["day_type"]), ("0", "mon,tue,wed,thu,fri,hol"))
        self.assertEqual(weekday["bands"]["09-10"], {"before": 0, "after": 1})

    def test_pattern_edit_uses_places(self):
        line = self.line("2")
        (pattern,) = line["patterns"]
        self.assertEqual([(e["kind"], [self.place_name(i) for i in e["places"]]) for e in pattern["edits"]],
                         [("inserted", ["新団地"]), ("removed", ["旧団地"])])
        self.assertEqual({e["trips"] for e in pattern["edits"]}, {1})  # seen on one paired trip

    def test_irregular_services(self):
        irr = self.report["service_days"]["irregular"]
        self.assertEqual([(x["service_id"], x["dates"], x["trips"], x["mode"]) for x in irr["old"]],
                         [("HOL", [{"start": "2026-05-15", "end": "2026-05-15"}], 1, "adds")])
        self.assertEqual(irr["new"], [])

    def test_route_geometry(self):
        (g1,) = self.line("1")["geometry"]
        self.assertTrue(g1["identical"])  # the dominant stop sequence is the same
        self.assertEqual((g1["old"], g1["new"]["source"], g1["change"]["max_m"]), (None, "stops", 0))
        self.assertEqual((self.place_name(g1["from"]), self.place_name(g1["to"])), ("駅前", "病院"))
        (g2,) = self.line("2")["geometry"]
        self.assertFalse(g2["identical"])
        self.assertTrue(g2["change"]["capped"])  # the new end stop is 8 km away
        self.assertGreater(g2["change"]["diverged_new_m"], 0)

    def test_trip_moved_to_another_line(self):
        (move,) = self.report["moves"]
        self.assertEqual((move["old"]["line"], move["new"]["line"], move["kind"]), ("1", "3", "rerouted"))
        self.assertEqual([(e["kind"], [self.place_name(i) for i in e["places"]]) for e in move["edits"]], [("extended", ["病院"])])
        self.assertEqual(self.line("1")["related"], ["3"])
        self.assertEqual(self.line("3")["status"], "added")

    def test_places_and_serving_lines(self):
        by_name = {self.place_name(i): p for i, p in enumerate(self.report["places"])}
        self.assertEqual(by_name["市役所前"]["status"], "renamed")
        self.assertEqual(by_name["旧団地"]["status"], "removed")
        self.assertEqual(by_name["新団地"]["lines"], ["2"])

    def test_every_raw_difference_is_accounted(self):
        cov = self.report["header"]["coverage"]
        files = self.report["accounting"]["files"]
        self.assertEqual(cov["raw_total"], sum(f["changes"] for f in files.values()))
        self.assertLess(cov["raw_total"], len(self.raw["changes"]))  # rows, not fields: T1 has three changed times
        self.assertEqual(cov["unclassified"], 0)
        self.assertEqual(cov["outside_comparison"], 0)  # X1 ran only on a special day, now compared by date
        self.assertNotIn({"code": "OUTSIDE_COMPARISON_PRESENT"}, self.report["header"]["notes"])
        # Only 2026-05-15 differs beyond the regular weekday change (shown in the line blocks): X1 is gone.
        groups = self.report["service_days"]["date_changes"]
        self.assertEqual([(g["dates"], g["added_count"], g["removed_count"], [r["departure"] for r in g["removed"]]) for g in groups],
                         [([{"start": "2026-05-15", "end": "2026-05-15"}], 0, 1, [720])])
        self.assertEqual(self.report["summary"]["date_changes"], 1)
        topics = {o["topic"] for o in self.report["other"]}
        self.assertEqual(topics, {"calendar_exceptions", "fares"})
        fares = next(o for o in self.report["other"] if o["topic"] == "fares")
        self.assertEqual([(d["kind"], d["column"], d["old"], d["new"]) for d in fares["details"]], [("field_changed", "price", "200", "220")])
        self.assertEqual(fares["truncated"], 0)
        stops = self.report["accounting"]["files"]["stops.txt"]
        self.assertEqual((stops["old_rows"], stops["new_rows"], stops["added"], stops["removed"], stops["changed_rows"]), (4, 4, 1, 1, 1))
        st = self.report["accounting"]["files"]["stop_times.txt"]
        self.assertGreater(st["changed_fields"], st["changed_rows"])
        self.assertEqual(self.report["accounting"]["files"]["agency.txt"]["changes"], 0)  # unchanged files are listed too


class RowsTest(unittest.TestCase):
    def test_merge_keeps_every_pattern_in_order(self):
        rows, mapping = _rows_for([tuple("ABC"), tuple("ABXC"), tuple("BC")])
        for pattern, positions in mapping.items():
            self.assertEqual(tuple(rows[i] for i in positions), pattern)
            self.assertEqual(len(positions), len(pattern))
        self.assertEqual(rows, list("ABXC"))

    def test_loop_visits_place_twice(self):
        rows, mapping = _rows_for([tuple("ABCA")])
        self.assertEqual(rows, list("ABCA"))
        self.assertEqual(mapping[tuple("ABCA")], [0, 1, 2, 3])


class AttributeDetailsTest(unittest.TestCase):
    def test_grouped_by_line_and_value(self):
        changes = [
            {"id": f"c{i:07d}", "file": "stop_times.txt", "kind": "field_changed", "key": [tid, str(i)], "column": "stop_headsign", "old": "駅", "new": "駅前"}
            for i, tid in enumerate(["T1", "T1", "T2"], 1)
        ] + [
            {"id": "c0000004", "file": "stops.txt", "kind": "field_changed", "key": ["S1"], "column": "stop_lat", "old": "40.1", "new": "40.10001"},
            {"id": "c0000005", "file": "trips.txt", "kind": "column_added", "column": "shape_id"},
        ]
        items, truncated = _attribute_details(changes, [c["id"] for c in changes], 10, {("trip", "T1"): "1", ("trip", "T2"): "1"})
        self.assertEqual(truncated, 0)
        self.assertEqual([(d["kind"], d["key"], d["column"], d["old"], d["new"], d["counts"]) for d in items], [
            ("attribute_changed", ["1"], "stop_headsign", "駅", "駅前", {"rows": 3}),
            ("coordinates_adjusted", None, "stop_lat", None, None, {"rows": 1}),  # ties: by file name
            ("column_added", None, "shape_id", None, None, {"rows": 1}),
        ])


class DateChangesTest(unittest.TestCase):
    def test_trips_without_a_time_at_some_stop(self):
        from datetime import date
        from types import SimpleNamespace
        d = date(2026, 5, 15)
        cal = lambda services: SimpleNamespace(days={d: frozenset(services)}, categories={d: "fri"})
        trips = {
            ("old", frozenset({"A"})): [Trip("x", "1", "0", ("P", "Q", "R"), (400, None, 410)), Trip("y", "1", "0", ("P", "R"), (500, None))],
            ("new", frozenset({"A"})): [Trip("x", "1", "0", ("P", "Q", "R"), (405, None, 415))],
        }
        groups, seen, differing = _date_changes(cal({"A"}), cal({"A"}), {}, lambda d: "fri", lambda side, s: trips[(side, s)], 10, 10, 45)
        self.assertEqual((differing, seen), (1, {"x", "y"}))
        (g,) = groups
        self.assertEqual((g["changed"], g["removed_count"]), ([{"line": "1", "departure": 405, "old_departure": 400}], 1))


class AccountingTest(unittest.TestCase):
    def test_buckets(self):
        raw = {"changes": [
            {"id": "c0000001", "file": "trips.txt", "kind": "column_removed", "column": "bikes_allowed"},
            {"id": "c0000002", "file": "frequencies.txt", "kind": "row_added", "key": ["T1", "06:00:00"]},
            {"id": "c0000003", "file": "fare_rules.txt", "kind": "row_removed", "key": None, "old": {}},
            {"id": "c0000004", "file": "stop_times.txt", "kind": "field_changed", "key": ["T9", "1"], "column": "arrival_time"},
            {"id": "c0000005", "file": "stop_times.txt", "kind": "field_changed", "key": ["T1", "1"], "column": "arrival_time"},
            {"id": "c0000006", "file": "routes_jp.txt", "kind": "file_added", "rows": 3},
            {"id": "c0000009", "file": "stops.txt", "kind": "field_changed", "key": ["S9"], "column": "stop_lon", "old": "140.5208490", "new": "140.520849"},
            {"id": "c0000010", "file": "stop_times.txt", "kind": "field_changed", "key": ["T9", "1"], "column": "arrival_time", "old": "7:30:00", "new": "07:30:00"},
            {"id": "c0000011", "file": "stops.txt", "kind": "field_changed", "key": ["S9"], "column": "stop_lat", "old": "40.1", "new": "40.10001"},
            {"id": "c0000012", "file": "stop_times.txt", "kind": "field_changed", "key": ["T5", "3"], "column": "stop_headsign", "old": "駅", "new": "駅前"},
            {"id": "c0000013", "file": "stop_times.txt", "kind": "row_removed", "key": ["T5", "9"], "old": {}},
            {"id": "c0000014", "file": "trips.txt", "kind": "column_removed", "column": "direction_id"},
            {"id": "c0000015", "file": "stop_times.txt", "kind": "row_removed", "key": ["T6", "9"], "old": {}},
            {"id": "c0000016", "file": "trips.txt", "kind": "field_changed", "key": ["T5"], "column": "route_id", "old": "10", "new": "1"},
            {"id": "c0000017", "file": "trips.txt", "kind": "field_changed", "key": ["T5"], "column": "route_id", "old": "10", "new": "2"},
        ]}
        raw["changes"] += [
            {"id": "c0000007", "file": "stop_times.txt", "kind": "field_changed", "key": ["T2", "1"], "column": "stop_id",
             "old": "i-1", "new": "1"},
            {"id": "c0000008", "file": "stop_times.txt", "kind": "field_changed", "key": ["T2", "2"], "column": "stop_id",
             "old": "i-2", "new": "3"},
        ]
        acc = classify(raw, Evidence(changed_trips={"T1"}, compared_trips={"T1", "T2", "T5", "T6"}, same_trips={"T5"}, route_line={"10": "L1", "1": "L1", "2": "L2"},
                                     old_stop_place={"i-1": "P1", "i-2": "P2"}, new_stop_place={"1": "P1", "3": "P3"}))
        # c7 renumbered stop, c8 another place; c13 renumbered stop_sequence of an unchanged trip;
        # c14 a core column; c15 a compared trip that is neither changed nor the same
        # c16 route renumbered within one line; c17 moved to another line
        self.assertEqual(sorted(acc.unclassified), ["c0000002", "c0000008", "c0000014", "c0000015", "c0000017"])
        self.assertEqual(acc.outside, ["c0000004"])
        self.assertEqual(acc.other, {"fares": ["c0000003"], "other_files": ["c0000006"], "formatting": ["c0000009", "c0000010"],
                                     "attributes": ["c0000001", "c0000011", "c0000012"]})


if __name__ == "__main__":
    unittest.main()
