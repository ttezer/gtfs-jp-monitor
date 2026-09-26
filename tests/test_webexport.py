import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from gtfs_jp_monitor.canonical import write_json
from gtfs_jp_monitor.catalog import sync_catalog
from gtfs_jp_monitor.store import change_path, generation_path
from gtfs_jp_semantic import ENGINE_VERSION
from gtfs_jp_semantic.rawdiff import gzip_bytes
from gtfs_jp_monitor.webexport import (build_export, compact_rules, estimate_pair, growth, read_stages, record_storage,
                                       storage_warnings)

from .schema_support import FIXTURES, load_json
from .test_catalog import FEED, ORG, FakeClient, feed_record, gen, uid

KEY = "v0.14.0__auto"


class WebExportTest(unittest.TestCase):
    def test_exports_analysed_generations_oldest_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gens = [gen(3, "current", "2026-04-01"), gen(2, "prev_1", "2025-10-01"), gen(1, "prev_2", "2025-04-01")]
            sync_catalog(FakeClient([feed_record(), feed_record(org="empty-org", feed="Empty")], {(ORG, FEED): gens}), root)
            base = load_json(FIXTURES / "generation" / "complete.json")
            for n in (1, 3):  # uid(2) not analysed yet
                doc = copy.deepcopy(base)
                doc["generation"]["uid"] = uid(n)
                write_json(generation_path(root, ORG, FEED, uid(n), KEY), doc)
            export, files = build_export(root, KEY)
        self.assertEqual(export["schema"], "gtfs-jp-monitor-web-export/1")
        self.assertEqual([f["feed_id"] for f in export["feeds"]], [FEED])  # feeds without analyses are left out
        feed = export["feeds"][0]
        self.assertEqual([g["uid"] for g in feed["generations"]], [uid(1), uid(3)])
        g = feed["generations"][1]
        self.assertEqual(g["rid"], "current")
        self.assertNotIn("pair", feed["generations"][0])  # the first publication forms no pair
        self.assertEqual(g["pair"], "U/NOT_REPORTED")
        self.assertEqual((g["rules"]["JPN_030"], export["rule_meta"]["JPN_030"]), (4, ["MEDIUM", "QUALITY"]))
        self.assertEqual(export["rule_titles"], {"tr": {}, "en": {}, "ja": {}})
        self.assertEqual((export["report_index"], files), ({}, {}))
        status = export["status"]
        self.assertEqual((status["backlog"], status["last_run"]), ({"unanalysed": 1, "reports_pending": 0}, None))
        self.assertEqual(status["storage"]["reports"], {"count": 0, "bytes": 0, "max_bytes": 0, "avg_bytes": 0})
        self.assertGreater(status["storage"]["analyses_bytes"], 0)
        self.assertRegex(status["built_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_indexes_stored_reports_one_file_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_catalog(FakeClient([feed_record()], {(ORG, FEED): [gen(1, "current", "2026-04-01")]}), root)
            doc = copy.deepcopy(load_json(FIXTURES / "generation" / "complete.json"))
            doc["generation"]["uid"] = uid(1)
            write_json(generation_path(root, ORG, FEED, uid(1), KEY), doc)
            report = copy.deepcopy(load_json(FIXTURES / "semantic" / "report-example.json"))
            report["header"]["feed"] = {"org_id": ORG, "feed_id": FEED}
            h = report["header"]
            path = change_path(root, ORG, FEED, ENGINE_VERSION, h["old"]["uid"], h["new"]["uid"])
            path.parent.mkdir(parents=True)
            path.write_bytes(gzip_bytes(report))
            export, files = build_export(root, KEY)
        pair = f"{h['old']['uid']}__{h['new']['uid']}"
        entry = export["report_index"][pair]
        self.assertEqual(entry["feed"], [ORG, FEED])
        self.assertEqual(entry["summary"], report["summary"])
        self.assertIn(entry["classification"]["class"], ("MEANINGFUL_SERVICE_CHANGE", "TECHNICAL_OR_METADATA_ONLY"))
        self.assertEqual(files, {f"{ORG}/{FEED}/{pair}": report})

    def test_newest_stored_engine_version_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync_catalog(FakeClient([feed_record()], {(ORG, FEED): [gen(1, "current", "2026-04-01")]}), root)
            doc = copy.deepcopy(load_json(FIXTURES / "generation" / "complete.json"))
            doc["generation"]["uid"] = uid(1)
            write_json(generation_path(root, ORG, FEED, uid(1), KEY), doc)
            report = copy.deepcopy(load_json(FIXTURES / "semantic" / "report-example.json"))
            report["header"]["feed"] = {"org_id": ORG, "feed_id": FEED}
            h = report["header"]
            for version, marker in (("0.0.1", "old"), (ENGINE_VERSION, "new"), ("99.0.0", "future")):
                r = copy.deepcopy(report)
                r["header"]["engine"]["version"] = version if version != "99.0.0" else "99.0.0"
                r["header"]["old"]["memo"] = marker
                path = change_path(root, ORG, FEED, version, h["old"]["uid"], h["new"]["uid"])
                path.parent.mkdir(parents=True)
                path.write_bytes(gzip_bytes(r))
            _, files = build_export(root, KEY)
        (doc,) = files.values()
        self.assertEqual(doc["header"]["old"]["memo"], "new")  # newest not above this engine


class CompactRulesTest(unittest.TestCase):
    def test_counts_only_unless_a_publication_differs(self):
        feeds = [{"generations": [{"rules": {"A": [2, "INFO", "SPEC"], "B": [1, "LOW", "QUALITY"]}},
                                  {"rules": {"A": [5, "INFO", "SPEC"]}},
                                  {"rules": {"A": [1, "CRITICAL", "SPEC"]}}]}]
        self.assertEqual(compact_rules(feeds), {"A": ["INFO", "SPEC"], "B": ["LOW", "QUALITY"]})
        self.assertEqual([g["rules"] for g in feeds[0]["generations"]],
                         [{"A": 2, "B": 1}, {"A": 5}, {"A": [1, "CRITICAL", "SPEC"]}])


class EstimateTest(unittest.TestCase):
    def test_changed_files_decide(self):
        rec = lambda files: {"files": {n: {"method": "canonical_csv", "hash": v} for n, v in files.items()}}
        old = rec({"feed_info.txt": "a", "calendar.txt": "b", "stops.txt": "c"})
        calendar_only = rec({"feed_info.txt": "x", "calendar.txt": "y", "stops.txt": "c"})
        stops_too = rec({"feed_info.txt": "x", "calendar.txt": "b", "stops.txt": "z"})
        new_file = rec({"feed_info.txt": "a", "calendar.txt": "b", "stops.txt": "c", "extra.txt": "q"})
        self.assertEqual([estimate_pair(old, x) for x in (calendar_only, stops_too, new_file)], ["~T", "~M", "~M"])
        self.assertIsNone(estimate_pair(old, {"files": None}))


class StorageTest(unittest.TestCase):
    def test_history_and_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "feeds").mkdir()
            (root / "feeds" / "a.bin").write_bytes(b"x" * 1000)
            record_storage(root, 100, dt.date(2026, 9, 1))
            record_storage(root, 120, dt.date(2026, 9, 1))  # the same day is replaced
            (root / "feeds" / "b.bin").write_bytes(b"y" * (3 << 20))
            record_storage(root, 100 + 10 * 1024, dt.date(2026, 9, 11))
            history = json.loads((root / "status" / "storage-history.json").read_text())
        self.assertEqual([(h["date"], h["repo_kb"]) for h in history], [("2026-09-01", 120), ("2026-09-11", 10340)])
        g = growth(history, dt.date(2026, 9, 20))
        self.assertEqual((g["from"], g["to"], g["repo_mb_per_day"]), ("2026-09-01", "2026-09-11", 1.0))
        self.assertGreater(g["data_mb_per_day"], 0.29)
        self.assertIsNone(growth(history, dt.date(2026, 10, 5)))  # only one entry in the last 30 days

    def test_stage_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stages.txt"
            path.write_text("analysis start 2026-09-27T19:10:00Z\nanalysis end 2026-09-27T19:40:30Z\ncommit start 2026-09-27T20:00:00Z\nnoise\n")
            self.assertEqual(read_stages(path), {
                "analysis": {"started": "2026-09-27T19:10:00Z", "finished": "2026-09-27T19:40:30Z", "minutes": 30.5},
                "commit": {"started": "2026-09-27T20:00:00Z"}})

    def test_warns_past_three_quarters_of_a_budget(self):
        self.assertEqual(storage_warnings({"site_bytes": 200 << 20}, 700 << 20), [])
        self.assertEqual(storage_warnings({"site_bytes": 800 << 20}, None),
                         ["web site is 800 MiB, 78% of its 1024 MiB budget"])
        self.assertEqual(len(storage_warnings({}, 900 << 20)), 1)


if __name__ == "__main__":
    unittest.main()
