import copy
import tempfile
import unittest
from pathlib import Path

from gtfs_jp_monitor.canonical import write_json
from gtfs_jp_monitor.diff import DiffError, build_diff, consecutive_pairs, sync_feed_diffs
from gtfs_jp_monitor.store import diff_path, generation_path

from .schema_support import FIXTURES, HAVE_JSONSCHEMA, errors, load_json, validator

KEY = "v0.14.0__auto"
ORG, FEED = "nagai-unyu", "Nagaibus"


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


BASE = load_json(FIXTURES / "generation" / "complete.json")


def gen_doc(n: int, **changes) -> dict:
    doc = copy.deepcopy(BASE)
    doc["generation"]["uid"] = uid(n)
    for path, value in changes.items():
        target = doc
        *parents, leaf = path.split(".")
        for p in parents:
            target = target[p]
        target[leaf] = value
    return doc


def index(entries: list[tuple[int, str | None, str]]) -> dict:
    """entries: (n, validation_status or None, source_status)."""
    gens = []
    for n, status, source in entries:
        analyses = [] if status is None else [{"release_tag": "v0.14.0", "gtfs_jp_profile": "auto", "validation_status": status}]
        gens.append({"uid": uid(n), "from_date": "2026-01-01", "to_date": "2026-12-31", "published_at": None,
                     "source_status": source, "analyses": analyses, "canonical": None})
    return {"org_id": ORG, "feed_id": FEED, "generations": gens}


class BuildDiffTest(unittest.TestCase):
    def test_scores_metrics_files_and_rules(self):
        old = gen_doc(1, **{"scores.publish": 96.2, "metrics.routes": 22, "metrics.avg_daily_trips": 198.4})
        old["rules"] = {"CAL_013": {"count": 3, "severity": "LOW", "class": "SPEC"},
                        "STP_037": {"count": 12, "severity": "LOW", "class": "QUALITY"},
                        "FRQ_002": {"count": 1, "severity": "INFO", "class": "ANALYTICS"},
                        "AAA_001": {"count": 1, "severity": "INFO", "class": "SPEC"}}
        old["file_row_counts"] = {"stops.txt": 398, "shapes.txt": 10}
        new = gen_doc(2, **{"scores.publish": 98.1})
        new["rules"] = {"JPN_030": {"count": 4, "severity": "MEDIUM", "class": "QUALITY"},
                        "STP_037": {"count": 5, "severity": "LOW", "class": "QUALITY"},
                        "FRQ_002": {"count": 1, "severity": "INFO", "class": "ANALYTICS"},
                        "AAA_001": {"count": 2, "severity": "INFO", "class": "SPEC"}}
        new["file_row_counts"] = {"stops.txt": 401, "translations.txt": 650}
        d = build_diff(old, new)
        self.assertEqual(d["scores"]["publish"], {"before": 96.2, "after": 98.1, "delta": 1.9})
        self.assertEqual(d["metrics"]["routes"], {"before": 22, "after": 24, "delta": 2})
        self.assertEqual(d["metrics"]["avg_daily_trips"]["delta"], 1.6)
        self.assertEqual(d["files"]["shapes.txt"], {"before": 10, "after": None, "delta": -10})
        self.assertEqual(d["files"]["translations.txt"], {"before": None, "after": 650, "delta": 650})
        self.assertEqual(d["rules"]["fixed"], {"CAL_013": {"before": 3, "after": 0}})
        self.assertEqual(d["rules"]["new"], {"JPN_030": {"before": 0, "after": 4}})
        self.assertEqual(d["rules"]["decreased"], {"STP_037": {"before": 12, "after": 5}})
        self.assertEqual(d["rules"]["increased"], {"AAA_001": {"before": 1, "after": 2}})
        self.assertEqual(d["rules"]["same"], {"FRQ_002": {"before": 1, "after": 1}})
        if HAVE_JSONSCHEMA:
            self.assertEqual(errors(validator("diff.schema.json"), d), [])

    def test_refusals(self):
        cases = {
            "not complete": (gen_doc(1, validation_status="PARTIAL"), gen_doc(2)),
            "same uid": (gen_doc(1), gen_doc(1)),
            "other feed": (gen_doc(1), gen_doc(2, **{"feed.feed_id": "Other"})),
            "other binary": (gen_doc(1), gen_doc(2, **{"analyzer.binary_sha256": "2" * 64})),
            "other profile": (gen_doc(1), gen_doc(2, **{"analyzer.gtfs_jp_profile": "v3"})),
            "other platform": (gen_doc(1), gen_doc(2, **{"analyzer.platform": {"os": "macos", "arch": "aarch64"}})),
        }
        for label, (old, new) in cases.items():
            with self.subTest(label=label), self.assertRaises(DiffError):
                build_diff(old, new)

    def test_validate_date_difference_is_allowed(self):
        # Different generations always have different validate dates (their from_date).
        build_diff(gen_doc(1, **{"analyzer.validate_date": "2025-10-01"}), gen_doc(2))


class PairsTest(unittest.TestCase):
    def test_skips_non_complete_between_neighbours(self):
        idx = index([(1, "COMPLETE", "AVAILABLE"), (2, "FATAL", "AVAILABLE"), (3, None, "SOURCE_UNAVAILABLE"),
                     (4, "COMPLETE", "SOURCE_UNAVAILABLE"), (5, "PARTIAL", "AVAILABLE"), (6, "COMPLETE", "AVAILABLE"),
                     (7, "COMPLETE", "ORDERING_UNKNOWN")])
        pairs = [(p.old_uid, p.new_uid, p.skipped_uids) for p in consecutive_pairs(idx, KEY)]
        self.assertEqual(pairs, [(uid(1), uid(4), (uid(2), uid(3))), (uid(4), uid(6), (uid(5),))])

    def test_leading_non_complete_is_not_skipped_into_a_pair(self):
        idx = index([(1, "FATAL", "AVAILABLE"), (2, "COMPLETE", "AVAILABLE"), (3, "COMPLETE", "AVAILABLE")])
        self.assertEqual([p.skipped_uids for p in consecutive_pairs(idx, KEY)], [()])

    def test_other_keys_are_ignored(self):
        idx = index([(1, "COMPLETE", "AVAILABLE"), (2, "COMPLETE", "AVAILABLE")])
        self.assertEqual(consecutive_pairs(idx, "v0.15.0__auto"), [])


class SyncDiffsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, *ns):
        for n in ns:
            write_json(generation_path(self.root, ORG, FEED, uid(n), KEY), gen_doc(n))

    def test_insertion_in_the_middle_replaces_the_stale_pair(self):
        self.store(1, 2, 3)
        first = sync_feed_diffs(self.root, index([(1, "COMPLETE", "AVAILABLE"), (3, "COMPLETE", "AVAILABLE")]), KEY)
        self.assertEqual(len(first.written), 1)
        stale = diff_path(self.root, ORG, FEED, KEY, uid(1), uid(3))
        self.assertTrue(stale.is_file())
        second = sync_feed_diffs(self.root, index([(1, "COMPLETE", "AVAILABLE"), (2, "COMPLETE", "AVAILABLE"),
                                                   (3, "COMPLETE", "AVAILABLE")]), KEY)
        self.assertEqual(len(second.written), 2)
        self.assertEqual(second.deleted, [str(stale.relative_to(self.root))])
        self.assertFalse(stale.exists())

    def test_rerun_is_a_no_op_and_foreign_files_survive(self):
        self.store(1, 2)
        idx = index([(1, "COMPLETE", "AVAILABLE"), (2, "COMPLETE", "AVAILABLE")])
        sync_feed_diffs(self.root, idx, KEY)
        foreign = self.root / "feeds" / ORG / FEED / "diffs" / KEY / "README.json"
        foreign.write_text("{}")
        again = sync_feed_diffs(self.root, idx, KEY)
        self.assertEqual((again.written, again.deleted), ([], []))
        self.assertTrue(foreign.exists())

    def test_identity_mismatch_is_refused_not_written(self):
        write_json(generation_path(self.root, ORG, FEED, uid(1), KEY), gen_doc(1))
        write_json(generation_path(self.root, ORG, FEED, uid(2), KEY), gen_doc(2, **{"analyzer.binary_sha256": "2" * 64}))
        res = sync_feed_diffs(self.root, index([(1, "COMPLETE", "AVAILABLE"), (2, "COMPLETE", "AVAILABLE")]), KEY)
        self.assertEqual(res.written, [])
        self.assertEqual(len(res.refused), 1)


if __name__ == "__main__":
    unittest.main()
