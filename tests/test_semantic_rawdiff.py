import gzip
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from gtfs_jp_semantic.rawdiff import diff_zips, gzip_bytes
from gtfs_jp_semantic.reader import Config

CONFIG = Config(bulk_threshold=3, max_rows_per_file=1000, max_uncompressed_bytes=10_000_000)


class RawDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def zip(self, name, members):
        path = self.dir / name
        with zipfile.ZipFile(path, "w") as zf:
            for member, data in members.items():
                zf.writestr(member, data)
        return path

    def diff(self, old, new, config=CONFIG):
        return diff_zips(self.zip("old.zip", old), self.zip("new.zip", new), config)

    def kinds(self, doc, file=None):
        return [(c["kind"], c.get("key"), c.get("column")) for c in doc["changes"] if file is None or c["file"] == file]

    def test_keyed_rows_and_cells(self):
        doc = self.diff(
            {"stops.txt": "stop_id,stop_name,stop_lat\nS1,A,35.0\nS2,B,35.1\nS3,C,35.2\n"},
            {"stops.txt": "stop_id,stop_name,stop_lat\nS1,A,35.0\nS2,B2,35.15\nS4,D,35.3\n"},
        )
        self.assertEqual(self.kinds(doc), [
            ("row_added", ["S4"], None),
            ("row_removed", ["S3"], None),
            ("field_changed", ["S2"], "stop_lat"),
            ("field_changed", ["S2"], "stop_name"),
        ])
        change = doc["changes"][2]
        self.assertEqual((change["old"], change["new"], change["id"]), ("35.1", "35.15", "c0000003"))
        self.assertEqual(doc["files"]["stops.txt"]["mode"], "keyed")

    def test_composite_key_and_columns(self):
        doc = self.diff(
            {"stop_times.txt": "trip_id,stop_sequence,arrival_time,old_col\nT1,1,08:00:00,x\nT1,2,08:05:00,x\n"},
            {"stop_times.txt": "trip_id,stop_sequence,arrival_time,new_col\nT1,1,08:00:00,y\nT1,2,08:06:00,y\n"},
        )
        self.assertEqual(self.kinds(doc), [
            ("column_added", None, "new_col"),
            ("column_removed", None, "old_col"),
            ("field_changed", ["T1", "2"], "arrival_time"),
        ])

    def test_files_added_removed_and_unchanged(self):
        doc = self.diff({"agency.txt": "agency_id\nA\n", "old.txt": "x\n1\n"},
                        {"agency.txt": "agency_id\nA\n", "new.txt": "y\n1\n2\n"})
        self.assertEqual(self.kinds(doc), [("file_added", None, None), ("file_removed", None, None)])
        self.assertEqual([c["rows"] for c in doc["changes"]], [2, 1])
        self.assertEqual(doc["files"]["agency.txt"]["counts"], {})

    def test_multiset_fallbacks(self):
        doc = self.diff(
            {"fare_rules.txt": "fare_id,route_id\nF1,R1\nF1,R1\nF2,R2\n",
             "stops.txt": "stop_id,stop_name\nS1,A\nS1,A2\n",
             "routes.txt": "route_short_name\nX\n"},
            {"fare_rules.txt": "fare_id,route_id\nF1,R1\nF2,R3\n",
             "stops.txt": "stop_id,stop_name\nS1,A\n",
             "routes.txt": "route_short_name\nY\n"},
        )
        reasons = {f: m["fallback_reason"] for f, m in doc["files"].items()}
        self.assertEqual(reasons, {"fare_rules.txt": "no_primary_key", "stops.txt": "duplicate_key",
                                   "routes.txt": "key_column_missing"})
        fare = [(c["kind"], c.get("old") or c.get("new")) for c in doc["changes"] if c["file"] == "fare_rules.txt"]
        self.assertEqual(fare, [("row_added", {"fare_id": "F2", "route_id": "R3"}),
                                ("row_removed", {"fare_id": "F1", "route_id": "R1"}),
                                ("row_removed", {"fare_id": "F2", "route_id": "R2"})])

    def test_optional_key_columns(self):
        doc = self.diff({"transfers.txt": "from_stop_id,to_stop_id,transfer_type\nA,B,0\n"},
                        {"transfers.txt": "from_stop_id,to_stop_id,transfer_type\nA,B,2\n"})
        self.assertEqual(doc["files"]["transfers.txt"]["key"], ["from_stop_id", "to_stop_id"])
        self.assertEqual(self.kinds(doc), [("field_changed", ["A", "B"], "transfer_type")])

    def test_single_row_feed_info(self):
        doc = self.diff({"feed_info.txt": "feed_version,feed_start_date\n1,20260401\n"},
                        {"feed_info.txt": "feed_version,feed_start_date\n2,20261001\n"})
        self.assertEqual(self.kinds(doc), [("field_changed", [], "feed_start_date"), ("field_changed", [], "feed_version")])

    def test_bulk_only_for_allowed_files(self):
        old_fares = "fare_id,route_id\n" + "".join(f"F{i},R{i}\n" for i in range(5))
        new_fares = "fare_id,route_id\n" + "".join(f"F{i},X{i}\n" for i in range(5))
        old_stops = "stop_id\n" + "".join(f"S{i}\n" for i in range(5))
        doc = self.diff({"fare_rules.txt": old_fares, "stops.txt": old_stops}, {"fare_rules.txt": new_fares, "stops.txt": "stop_id\n"})
        fare = [c for c in doc["changes"] if c["file"] == "fare_rules.txt"]
        self.assertEqual([c["kind"] for c in fare], ["rows_bulk"])
        self.assertEqual(fare[0]["counts"], {"added": 5, "removed": 5, "changed_rows": 0, "changed_cells": 0})
        self.assertEqual(len([c for c in doc["changes"] if c["file"] == "stops.txt"]), 5)  # never bulked

    def test_opaque_files(self):
        doc = self.diff({"a.txt": b"id\n\xff\xfe\x81\n"}, {"a.txt": b"id\n\xff\xfe\x82\n"})
        self.assertEqual(self.kinds(doc), [("file_changed_opaque", None, None)])
        same = self.diff({"a.txt": b"id\n\xff\xfe\x81\n"}, {"a.txt": b"id\n\xff\xfe\x81\n"})
        self.assertEqual(same["changes"], [])

    def test_values_are_compared_exactly(self):
        doc = self.diff({"stops.txt": "stop_id,stop_name\nS1,A\n"}, {"stops.txt": "stop_id,stop_name\nS1,A \n"})
        self.assertEqual(self.kinds(doc), [("field_changed", ["S1"], "stop_name")])

    def test_identical_feeds_and_determinism(self):
        members = {"stops.txt": "stop_id,stop_name\nS2,B\nS1,A\n", "trips.txt": "trip_id\nT1\n"}
        same = self.diff(members, members)
        self.assertEqual(same["changes"], [])
        a = self.diff({"stops.txt": "stop_id\nS1\nS2\n"}, {"stops.txt": "stop_id\nS3\nS2\n"})
        b = self.diff({"stops.txt": "stop_id\nS2\nS1\n"}, {"stops.txt": "stop_id\nS2\nS3\n"})
        self.assertEqual(a["changes"], b["changes"])  # source row order does not matter
        self.assertNotEqual(a["old"]["sha256"], b["old"]["sha256"])  # but the archives themselves differ
        old, new = self.zip("o2.zip", {"stops.txt": "stop_id\nS1\n"}), self.zip("n2.zip", {"stops.txt": "stop_id\nS2\n"})
        self.assertEqual(gzip_bytes(diff_zips(old, new, CONFIG)), gzip_bytes(diff_zips(old, new, CONFIG)))
        self.assertEqual(json.loads(gzip.decompress(gzip_bytes(a))), a)

    def test_archive_warnings_are_kept(self):
        doc = self.diff({"stops.txt": "stop_id\n", "x/y.txt": "z\n"}, {"stops.txt": "stop_id\n"})
        self.assertEqual(doc["archive_warnings"], [{"code": "archive_layout", "member": "x/y.txt", "side": "old"}])


if __name__ == "__main__":
    unittest.main()
