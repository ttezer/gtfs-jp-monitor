import dataclasses
import tempfile
import unittest
from pathlib import Path

from gtfs_jp_semantic.events import evidence_index, ranges
from gtfs_jp_semantic.reader import Config
from gtfs_jp_semantic.report import build_report

from .schema_support import HAVE_JSONSCHEMA, errors, validator
from .test_semantic_report import NEW, OLD, PUB, write_zip

KWARGS = dict(feed={"org_id": "sample-city", "feed_id": "SampleBus"}, old_pub=dict(PUB, uid="1c8d1613-0633-4b70-9268-c88785f29ac3"),
              new_pub=dict(PUB, uid="2da131b0-cb13-4bc2-b8eb-d925bf6050ce"))


def build(old, new, events=True):
    base = Config.load()
    config = dataclasses.replace(base, report=dict(base.report, events=events))
    with tempfile.TemporaryDirectory() as tmp:
        return build_report(write_zip(Path(tmp) / "old.zip", old), write_zip(Path(tmp) / "new.zip", new), config=config, **KWARGS)


def expand(evidence):
    out = []
    for item in evidence:
        first, _, last = item.partition("-")
        out += [f"c{n:07d}" for n in range(int(first[1:]), int((last or first)[1:]) + 1)]
    return out


class RangesTest(unittest.TestCase):
    def test_runs(self):
        self.assertEqual(ranges(["c0000003", "c0000001", "c0000002", "c0000007", "c0000009", "c0000010"]),
                         ["c0000001-c0000003", "c0000007", "c0000009-c0000010"])
        self.assertEqual(ranges([]), [])

    def test_index_by_entity(self):
        raw = {"changes": [
            {"id": "c0000001", "file": "stops.txt", "kind": "field_changed", "key": ["S1"]},
            {"id": "c0000002", "file": "stop_times.txt", "kind": "row_added", "key": ["T1", "3"]},
            {"id": "c0000003", "file": "fare_rules.txt", "kind": "row_added", "key": None, "new": {"fare_id": "F"}},
            {"id": "c0000004", "file": "trips.txt", "kind": "row_removed", "key": None, "old": {"trip_id": "T9"}},
        ]}
        self.assertEqual(dict(evidence_index(raw)), {("stops.txt", "S1"): ["c0000001"], ("stop_times.txt", "T1"): ["c0000002"],
                                                     ("trips.txt", "T9"): ["c0000004"]})


class EventsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report, cls.raw = build(OLD, NEW)

    def by_type(self, kind):
        return [e for e in self.report["events"] if e["type"] == kind]

    def test_only_with_the_setting(self):
        report, _ = build(OLD, NEW, events=False)
        self.assertNotIn("events", report)
        self.assertNotIn("mappings", report)

    def test_valid(self):
        if HAVE_JSONSCHEMA:
            self.assertEqual(errors(validator("semantic-report.schema.json"), self.report), [])

    def test_every_conclusion_is_an_event(self):
        self.assertEqual(sorted(e["type"] for e in self.report["events"]), sorted([
            "STOP_RENAMED", "STOP_REMOVED", "STOP_ADDED",  # 市役所 -> 市役所前, 旧団地 removed, 新団地 added
            "ROUTE_ADDED",  # line 3
            "STOP_INSERTED", "STOP_SKIPPED",  # line 2 now serves 新団地 instead of 旧団地
            "SERVICE_INCREASED",  # line 3 gets its first trip
            "TRIPS_RETIMED", "FIRST_LAST_CHANGED",  # line 1: T1 five minutes later
            "TRIPS_MOVED",  # T4 from line 1 to line 3
            "OTHER_CHANGED", "FARE_CHANGED"]))
        moved = self.by_type("TRIPS_MOVED")[0]
        self.assertEqual((moved["line"], moved["to_line"], moved["count"]), ("1", "3", 1))
        service = self.by_type("SERVICE_INCREASED")[0]
        self.assertEqual((service["line"], service["before"], service["after"]), ("3", 0, 1))

    def test_evidence_is_the_rows_behind_each_event(self):
        changes = {c["id"]: c for c in self.raw["changes"]}
        for e in self.report["events"]:
            with self.subTest(event=e["type"]):
                ids = expand(e["evidence"])
                self.assertTrue(ids)
                self.assertTrue(set(ids) <= set(changes))
                files = {changes[i]["file"] for i in ids}
                if e["type"].startswith("STOP_") and "place" in e:
                    self.assertEqual(files, {"stops.txt"})
        renamed = self.by_type("STOP_RENAMED")[0]
        (change,) = [changes[i] for i in expand(renamed["evidence"])]
        self.assertEqual((change["key"], change["column"], change["new"]), (["S2"], "stop_name", "市役所前"))
        self.assertEqual(self.report["places"][renamed["place"]]["new"]["name"], "市役所前")

    def test_ids_that_changed_are_mapped(self):
        renumbered = dict(NEW, **{
            "stops.txt": NEW["stops.txt"].replace("S1,", "S01,"),
            "stop_times.txt": NEW["stop_times.txt"].replace(",S1,", ",S01,").replace("T2,", "T2b,"),
            "trips.txt": NEW["trips.txt"].replace(",T2,", ",T2b,"),
        })
        report, _ = build(OLD, renumbered)
        self.assertEqual(report["mappings"]["stops"], [{"old": ["S1"], "new": ["S01"]}])
        self.assertEqual(report["mappings"]["trips"], [["T2", "T2b"]])
        self.assertEqual(report["mappings"]["routes"], [])
        self.assertNotIn("STOP_ADDED", [e["type"] for e in report["events"] if e.get("place") is not None
                                         and report["places"][e["place"]]["new"] and report["places"][e["place"]]["new"]["stop_id"] == "S01"])


if __name__ == "__main__":
    unittest.main()
