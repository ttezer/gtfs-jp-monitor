import hashlib
import tempfile
import unittest
from pathlib import Path

from gtfs_jp_monitor.download import Downloaded, DownloadError
from gtfs_jp_monitor.semantic_compare import (apply_override, check_expect, compare, load_sample, summarise,
                                              summary_text, zip_path)
from gtfs_jp_semantic.reader import Config

from .test_semantic_report import NEW, OLD, PUB, write_zip

SAMPLE = Path(__file__).resolve().parent / "sample" / "semantic-pairs.json"
OLD_UID, NEW_UID = "1c8d1613-0633-4b70-9268-c88785f29ac3", "2da131b0-cb13-4bc2-b8eb-d925bf6050ce"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SampleTest(unittest.TestCase):
    def test_sample_file_loads_with_its_ground_truth(self):
        pairs = load_sample(SAMPLE)
        self.assertGreater(len(pairs), 300)
        truth = [p for p in pairs if p["group"] == "ground_truth"]
        self.assertEqual([p["feed"] for p in truth], ["toyama-asahitown/asahimachibus"])
        self.assertEqual(len(truth[0]["expect"]["line_status"]), 9)
        self.assertEqual(len({(p["old"]["uid"], p["new"]["uid"]) for p in pairs}), len(pairs))


class OverrideTest(unittest.TestCase):
    def test_sections_are_merged_key_by_key(self):
        base = Config.load()
        cfg = apply_override(base, {"matching": {"stop_group_radius_m": 150}, "min_overlap_days": 7})
        self.assertEqual(cfg.matching["stop_group_radius_m"], 150)
        self.assertEqual(cfg.matching["line_min_overlap"], base.matching["line_min_overlap"])
        self.assertEqual(cfg.report, base.report)
        self.assertEqual(cfg.min_overlap_days, 7)
        self.assertEqual(base.matching["stop_group_radius_m"], 200)  # the base is not changed

    def test_unknown_field_is_refused(self):
        with self.assertRaises(ValueError):
            apply_override(Config.load(), {"matchng": {}})


class ZipCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.source = write_zip(self.dir / "source.zip", OLD)
        self.pub = {"uid": OLD_UID, "sha256": sha(self.source)}

    def tearDown(self):
        self.tmp.cleanup()

    def fake_download(self, content: Path):
        calls = []

        def download(url, dest):
            calls.append(url)
            Path(dest).write_bytes(content.read_bytes())
            return Downloaded(Path(dest), sha(content), content.stat().st_size)
        return download, calls

    def test_missing_zip_is_downloaded_and_verified(self):
        download, calls = self.fake_download(self.source)
        cache = self.dir / "cache"
        cache.mkdir()
        path = zip_path(cache, "org/feed", self.pub, download=download)
        self.assertEqual(sha(path), self.pub["sha256"])
        self.assertEqual(calls, [f"https://api.gtfs-data.jp/v2/organizations/org/feeds/feed/files/feed.zip?uid={OLD_UID}"])
        zip_path(cache, "org/feed", self.pub, download=download)
        self.assertEqual(len(calls), 1)  # cached afterwards

    def test_other_content_is_refused(self):
        other = write_zip(self.dir / "other.zip", NEW)
        download, _ = self.fake_download(other)
        cache = self.dir / "cache"
        cache.mkdir()
        with self.assertRaises(ValueError):
            zip_path(cache, "org/feed", self.pub, download=download)
        self.assertEqual(list(cache.iterdir()), [])  # nothing is kept
        (cache / f"{OLD_UID}.zip").write_bytes(other.read_bytes())
        with self.assertRaises(ValueError):
            zip_path(cache, "org/feed", self.pub, download=download)


class CompareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        old, new = write_zip(d / f"{OLD_UID}.zip", OLD), write_zip(d / f"{NEW_UID}.zip", NEW)
        cls.pair = {"feed": "sample-city/SampleBus", "group": "control",
                    "old": dict(PUB, uid=OLD_UID, sha256=sha(old)), "new": dict(PUB, uid=NEW_UID, sha256=sha(new)),
                    "expect": {"line_status": {"2": "changed"}}}
        base = Config.load()
        # no place match is accepted: 市役所 → 市役所前 is no longer a rename, every changed place is removed + added
        candidate = apply_override(base, {"matching": {"accept_confidence": 1.1}})
        cls.result = compare([cls.pair], d, base, candidate)
        cls.dir = d

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_both_configurations_are_measured(self):
        r = self.result["pairs"][0]
        self.assertNotIn("error", r)
        self.assertEqual(r["base"]["places_renamed"], 1)
        self.assertEqual(r["candidate"]["places_renamed"], 0)
        self.assertEqual([r[n]["places_added"] for n in ("base", "candidate")], [1, 4])
        self.assertEqual([r[n]["trip_moves"] for n in ("base", "candidate")], [1, 0])
        self.assertEqual(r["base"]["expect_failures"], [])
        t = self.result["summary"]["totals"]
        self.assertEqual(t["candidate"]["places_removed"] - t["base"]["places_removed"], 3)
        self.assertEqual(self.result["summary"]["line_relations_changed"], [])  # lines are matched by name
        self.assertEqual(self.result["configs"]["candidate"]["matching"]["accept_confidence"], 1.1)
        self.assertIn("places_renamed", summary_text(self.result["summary"]))

    def test_failing_pair_is_reported_not_raised(self):
        broken = dict(self.pair, old=dict(self.pair["old"], sha256="0" * 64))
        result = compare([broken], self.dir, Config.load(), Config.load())
        self.assertEqual(len(result["summary"]["errors"]), 1)
        self.assertIn("SHA-256", result["summary"]["errors"][0]["error"])


class SummaryTest(unittest.TestCase):
    def m(self, relations, unmatched=0):
        counts = dict.fromkeys(("places_added", "places_removed", "places_renamed", "places_moved", "trip_moves",
                                "pattern_edits", "unclassified"), 0)
        return counts | {"unmatched_lines": unmatched, "lines_changed": 0, "relations": relations}

    def test_changed_line_relations_are_listed(self):
        same = [["renamed", "A"]]
        results = [
            {"feed": "o/f", "group": "g", "old": "a", "new": "b",
             "base": self.m([["added", "A"], ["discontinued", "A0"]], 2), "candidate": self.m(same)},
            {"feed": "o/f", "group": "g", "old": "b", "new": "c", "base": self.m(same), "candidate": self.m(same)},
            # a line that only stops being changed keeps its relation
            {"feed": "o/f", "group": "g", "old": "d", "new": "e",
             "base": self.m([["changed", "B"]]), "candidate": self.m([["unchanged", "B"]])},
            {"feed": "o/f", "group": "g", "old": "c", "new": "d", "error": "ValueError: x"},
        ]
        s = summarise(results)
        self.assertEqual(s["pairs"], 4)
        self.assertEqual(len(s["errors"]), 1)
        self.assertEqual(s["line_relations_changed"], [
            {"feed": "o/f", "group": "g", "old": "a", "new": "b", "unmatched_lines": [2, 0],
             "only_base": [["added", "A"], ["discontinued", "A0"]], "only_candidate": [["renamed", "A"]]}])
        self.assertEqual(s["totals"]["base"]["unmatched_lines"], 2)

    def test_expectations(self):
        m = {"lines": {"added": 0, "discontinued": 0, "renamed": 1, "merged": 0, "split": 0, "restructured": 0,
                       "changed": 0, "unchanged": 0}, "relations": [["renamed", "A"]]}
        self.assertEqual(check_expect(m, {"lines": {"renamed": 1}, "line_status": {"A": "renamed"}}), [])
        failures = check_expect(m, {"lines": {"renamed": 2}, "line_status": {"A": "merged", "B": "renamed"}})
        self.assertEqual(len(failures), 3)


if __name__ == "__main__":
    unittest.main()
