import copy
import gzip
import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from gtfs_jp_monitor.canonical import write_json
from gtfs_jp_monitor.catalog import load_catalog, sync_catalog
from gtfs_jp_monitor.changes import find_pending_reports, page_pairs, report_pairs, run_reports, window_uids
from gtfs_jp_monitor.download import DownloadError, Downloaded
from gtfs_jp_monitor.pipeline import _catalog_order
from gtfs_jp_monitor.signature import content_signature
from gtfs_jp_monitor.store import (analysis_digests, build_feed_index, change_path, content_path, generation_path, list_analyses,
                                 write_feed_index)

from .schema_support import FIXTURES, load_json
from .test_catalog import FEED, ORG, FakeClient, feed_record, gen, uid
from .test_semantic_report import NEW, OLD

KEY = "v0.14.0__auto"
FK = (ORG, FEED)


def zip_bytes(files: dict) -> bytes:
    path = Path(tempfile.mkstemp(suffix=".zip")[1])
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), text)
    data = path.read_bytes()
    path.unlink()
    return data


class FakeDownloader:
    def __init__(self, blobs: dict[str, bytes], failing: dict[str, DownloadError] | None = None):
        self.blobs, self.failing, self.calls = blobs, failing or {}, []

    def __call__(self, url: str, dest: Path) -> Downloaded:
        u = url.rsplit("uid=", 1)[1]
        self.calls.append(u)
        if u in self.failing:
            raise self.failing[u]
        dest.write_bytes(self.blobs[u])
        return Downloaded(dest, hashlib.sha256(self.blobs[u]).hexdigest(), len(self.blobs[u]))


class ReportRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        sync_catalog(FakeClient([feed_record()], {FK: [gen(3, "current", "2026-04-01"), gen(2, "prev_1", "2025-10-01"),
                                                       gen(1, "prev_2", "2025-04-01")]}), self.root)
        # uid(1) -> uid(2) changed; uid(3) republishes uid(2) with the same content in other ZIP bytes
        # (files in another order), so it is equivalent.
        self.blobs = {uid(1): zip_bytes(OLD), uid(2): zip_bytes(NEW), uid(3): zip_bytes(dict(reversed(list(NEW.items()))))}
        base = load_json(FIXTURES / "generation" / "complete.json")
        for n in (1, 2, 3):
            doc = copy.deepcopy(base)
            doc["generation"]["uid"] = uid(n)
            doc["generation"]["sha256"] = hashlib.sha256(self.blobs[uid(n)]).hexdigest()
            if n == 1:
                doc["metrics"]["routes"] += 1
            write_json(generation_path(self.root, *FK, uid(n), KEY), doc)
            self.sign(n)
        self.index()

    def tearDown(self):
        self.tmp.cleanup()

    def sign(self, n):
        path = self.root / "sign.zip"
        path.write_bytes(self.blobs[uid(n)])
        write_json(content_path(self.root, *FK, uid(n)), content_signature(path, hashlib.sha256(self.blobs[uid(n)]).hexdigest()))
        path.unlink()

    def index(self):
        feeds, gens = load_catalog(self.root)
        doc = build_feed_index(feeds[FK], _catalog_order(gens[FK]), list_analyses(self.root, *FK), KEY,
                               digests=analysis_digests(self.root, *FK))
        write_feed_index(self.root, doc)

    def test_builds_changed_pairs_once(self):
        dl = FakeDownloader(self.blobs)
        run = run_reports(self.root, KEY, downloader=dl, engine_version="0.1.0")
        self.assertEqual((run.counts["pending"], run.counts["reported"]), (1, 1))  # uid(2) -> uid(3) is equivalent
        path = change_path(self.root, *FK, "0.1.0", uid(1), uid(2))
        report = json.loads(gzip.decompress(path.read_bytes()))
        self.assertEqual((report["header"]["old"]["uid"], report["header"]["new"]["uid"]), (uid(1), uid(2)))
        self.assertEqual(sorted(dl.calls), [uid(1), uid(2)])
        again = run_reports(self.root, KEY, downloader=dl, engine_version="0.1.0")
        self.assertEqual(again.counts["pending"], 0)
        self.assertEqual(len(dl.calls), 2)

    def test_pairs_follow_the_page(self):
        index = {"generations": [
            {"uid": u, "source_status": "AVAILABLE", "analyses": [
                {"release_tag": "v0.14.0", "gtfs_jp_profile": "auto", "validation_status": st, "equivalent_to_previous": eq}] if st else []}
            for u, st, eq in [("a", "COMPLETE", False), ("b", None, False), ("c", "PARTIAL", False), ("d", "COMPLETE", True),
                              ("e", "FATAL", False), ("f", "COMPLETE", False), ("g", "COMPLETE", False)]]}
        # b unanalysed (passed over), d equivalent to c, e FATAL breaks the chain
        self.assertEqual(report_pairs(index, KEY), [("a", "c"), ("f", "g")])

    def test_page_pairs_beyond_neighbours(self):
        gens = lambda specs: {"generations": [
            {"uid": u, "source_status": "AVAILABLE", "analyses": [
                {"release_tag": "v0.14.0", "gtfs_jp_profile": "auto", "validation_status": st, "equivalent_to_previous": eq}]}
            for u, st, eq in specs]}
        # a b c d e(current) f(next); c2 equivalent to c; only c..f... is the window: e-3 = b
        index = gens([("a", "COMPLETE", False), ("b", "COMPLETE", False), ("c", "COMPLETE", False), ("c2", "COMPLETE", True),
                      ("d", "FATAL", False), ("e", "COMPLETE", False), ("f", "COMPLETE", False)])
        entries = {"e": {"rid_observed": "current"}, "f": {"rid_observed": "next_1"}}
        # groups: a, b, c+c2, d(FATAL), e, f; window from e-3 = b: b, c+c2, e, f (d FATAL left out)
        self.assertEqual(sorted(page_pairs(index, KEY, entries)), sorted([("b", "e"), ("b", "f"), ("c2", "e"), ("c2", "f")]))
        # Reports stay within the page: neighbours a -> b is older history, b -> c inside the window.
        self.assertEqual(window_uids(index, KEY, entries), {"b", "c", "c2", "e", "f"})
        u = {x: uid(i + 1) for i, x in enumerate(["a", "b", "c", "c2", "d", "e", "f"])}
        root = Path(self.tmp.name) / "window"
        real = json.loads(json.dumps(index))
        for g in real["generations"]:
            g["uid"] = u[g["uid"]]
        write_json(root / "feeds" / ORG / FEED / "feed.json", dict(real, org_id=ORG, feed_id=FEED))
        found = find_pending_reports(root, [FK], KEY, "0.1.0", {FK: {u[k]: v for k, v in entries.items()}})
        back = {v: k for k, v in u.items()}
        self.assertEqual({(back[p.old_uid], back[p.new_uid]) for p in found},
                         {("b", "c"), ("e", "f"), ("b", "e"), ("b", "f"), ("c2", "e"), ("c2", "f")})

    def test_new_engine_version_rebuilds(self):
        run_reports(self.root, KEY, downloader=FakeDownloader(self.blobs), engine_version="0.1.0")
        self.assertEqual(len(find_pending_reports(self.root, [FK], KEY, "0.2.0")), 1)

    def test_changed_source_is_marked_not_reported(self):
        blobs = dict(self.blobs, **{uid(2): zip_bytes(dict(NEW, **{"other.txt": "y\n"}))})
        run = run_reports(self.root, KEY, downloader=FakeDownloader(blobs), engine_version="0.1.0")
        self.assertEqual(run.counts["failed"], 1)
        marker = json.loads(change_path(self.root, *FK, "0.1.0", uid(1), uid(2), ".error.json").read_text())
        self.assertEqual(marker["code"], "SOURCE_CHANGED")
        self.assertFalse(change_path(self.root, *FK, "0.1.0", uid(1), uid(2)).exists())
        self.assertEqual(find_pending_reports(self.root, [FK], KEY, "0.1.0"), [])

    def test_engine_error_is_marked(self):
        blobs = dict(self.blobs, **{uid(1): b"not a zip"})
        doc = json.loads(generation_path(self.root, *FK, uid(1), KEY).read_text())
        doc["generation"]["sha256"] = hashlib.sha256(b"not a zip").hexdigest()
        write_json(generation_path(self.root, *FK, uid(1), KEY), doc)
        run = run_reports(self.root, KEY, downloader=FakeDownloader(blobs), engine_version="0.1.0")
        marker = json.loads(change_path(self.root, *FK, "0.1.0", uid(1), uid(2), ".error.json").read_text())
        self.assertEqual((run.counts["failed"], marker["code"]), (1, "ENGINE_ERROR"))

    def test_engine_error_is_retried_after_an_engine_change(self):
        self.test_engine_error_is_marked()
        self.assertEqual(find_pending_reports(self.root, [FK], KEY, "0.1.0"), [])  # same code: not retried
        path = change_path(self.root, *FK, "0.1.0", uid(1), uid(2), ".error.json")
        marker = json.loads(path.read_text())
        marker["engine_build"] = "older"
        write_json(path, marker)
        self.assertEqual(len(find_pending_reports(self.root, [FK], KEY, "0.1.0")), 1)
        write_json(path, dict(marker, code="SOURCE_CHANGED"))  # lasting failures stay marked
        self.assertEqual(find_pending_reports(self.root, [FK], KEY, "0.1.0"), [])

    def test_time_budget_defers_the_rest(self):
        ticks = iter([0, 100, 100])  # start, then before the only pair: the budget is spent
        run = run_reports(self.root, KEY, downloader=FakeDownloader(self.blobs), engine_version="0.1.0",
                          max_seconds=50, clock=lambda: next(ticks))
        self.assertEqual((run.counts["reported"], run.counts["deferred"]), (0, 1))
        self.assertEqual(len(find_pending_reports(self.root, [FK], KEY, "0.1.0")), 1)  # still pending

    def test_a_slow_pair_fails_alone(self):
        run = run_reports(self.root, KEY, downloader=FakeDownloader(self.blobs), engine_version="0.1.0", child_timeout=0.001)
        marker = json.loads(change_path(self.root, *FK, "0.1.0", uid(1), uid(2), ".error.json").read_text())
        self.assertEqual((run.counts["failed"], marker["code"]), (1, "ENGINE_ERROR"))
        self.assertTrue(marker["detail"].startswith("Timeout"))

    def test_transient_download_failure_is_retried(self):
        dl = FakeDownloader(self.blobs, failing={uid(1): DownloadError("DOWNLOAD_FAILED", "timeout")})
        run = run_reports(self.root, KEY, downloader=dl, engine_version="0.1.0")
        self.assertEqual(run.counts["failed"], 1)
        self.assertEqual(len(find_pending_reports(self.root, [FK], KEY, "0.1.0")), 1)


if __name__ == "__main__":
    unittest.main()
