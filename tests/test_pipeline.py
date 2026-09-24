import hashlib
import json
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from gtfs_jp_monitor.analyzer import AnalyzerBinary
from gtfs_jp_monitor.catalog import sync_catalog
from gtfs_jp_monitor.download import Downloaded, DownloadError
from gtfs_jp_monitor.pipeline import run_analysis
from gtfs_jp_monitor.store import UNPINNED_MARKER, StoreError, generation_path

from .schema_support import FIXTURES, HAVE_JSONSCHEMA, errors, validator
from .test_catalog import FEED, ORG, FakeClient, feed_record, gen, uid

OK_REPORT = FIXTURES / "analyzer" / "ok.json"

# Fake analyzer: the "ZIP" content selects the behaviour.
FAKE = textwrap.dedent(
    """\
    #!{python}
    import shutil, sys
    args = sys.argv[1:]
    if args == ["--version"]:
        print("gtfs-analyzer 0.14.0"); sys.exit(0)
    content = open(args[1], "rb").read()
    out = args[args.index("-o") + 1]
    if content.startswith(b"OK"):
        shutil.copyfile({ok!r}, out); sys.exit(1)
    if content.startswith(b"FATAL"):
        open(out, "w").write('{{"status":"fatal","code":"ZipUnreadable","message":"x"}}'); sys.exit(2)
    sys.exit(101)
    """
)


class FakeDownloader:
    def __init__(self, contents: dict[str, bytes], errors_: dict[str, DownloadError] | None = None):
        self.contents = contents
        self.errors = errors_ or {}
        self.calls: list[str] = []

    def __call__(self, url: str, dest: Path):
        uid_ = url.rsplit("uid=", 1)[1]
        self.calls.append(uid_)
        if uid_ in self.errors:
            raise self.errors.pop(uid_)
        data = self.contents[uid_]
        dest.write_bytes(data)
        return Downloaded(dest, hashlib.sha256(data).hexdigest(), len(data))


GENS = [gen(3, "current", "2026-04-01"), gen(2, "prev_1", "2025-10-01"), gen(1, "prev_2", "2025-04-01")]


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "data"
        self.data.mkdir()
        (self.data / UNPINNED_MARKER).write_text("")
        fake = base / "gtfs-analyzer"
        fake.write_text(FAKE.format(python=sys.executable, ok=str(OK_REPORT)), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.binary = AnalyzerBinary.inspect(fake)
        sync_catalog(FakeClient([feed_record()], {(ORG, FEED): GENS}), self.data)

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, downloader, **kwargs):
        return run_analysis(self.data, self.binary, "auto", downloader=downloader, **kwargs)

    def feed_dir(self) -> Path:
        return self.data / "feeds" / ORG / FEED

    def test_full_run_then_no_op(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(2): b"OK2", uid(3): b"OK3"})
        first = self.run_it(dl)
        self.assertEqual(first.counts["analyzed"], 3)
        self.assertEqual(sorted(p.name for p in (self.feed_dir() / "diffs" / "v0.14.0__auto").iterdir()),
                         [f"{uid(1)}__{uid(2)}.json", f"{uid(2)}__{uid(3)}.json"])
        self.assertIsNotNone(first.run_file)
        second = self.run_it(dl)
        self.assertEqual((second.counts["analyzed"], second.changed_files, second.run_file), (0, [], None))
        self.assertEqual(len(dl.calls), 3)  # nothing downloaded twice

    def test_interrupted_run_resumes_newest_first(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(2): b"OK2", uid(3): b"OK3"})
        first = self.run_it(dl, limit=1)
        self.assertEqual([i["uid"] for i in first.items], [uid(3)])
        self.assertEqual(first.counts["skipped"], 2)
        rest = self.run_it(dl)
        self.assertEqual([i["uid"] for i in rest.items], [uid(2), uid(1)])

    def test_transient_download_failure_is_retried(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(2): b"OK2", uid(3): b"OK3"},
                            {uid(2): DownloadError("DOWNLOAD_FAILED", "HTTP 503")})
        first = self.run_it(dl)
        self.assertEqual(first.counts["failed"], 1)
        self.assertFalse(generation_path(self.data, ORG, FEED, uid(2), "v0.14.0__auto").exists())
        second = self.run_it(dl)
        self.assertEqual([i["uid"] for i in second.items], [uid(2)])

    def test_source_unavailable_is_warned_not_stored(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(3): b"OK3"}, {uid(2): DownloadError("SOURCE_UNAVAILABLE", "HTTP 404")})
        result = self.run_it(dl)
        self.assertIn("SOURCE_UNAVAILABLE", [w["code"] for w in result.warnings])
        self.assertFalse(generation_path(self.data, ORG, FEED, uid(2), "v0.14.0__auto").exists())

    def test_fatal_generation_is_stored_and_skipped_in_diffs(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(2): b"FATAL", uid(3): b"OK3"})
        self.run_it(dl)
        doc = json.loads(generation_path(self.data, ORG, FEED, uid(2), "v0.14.0__auto").read_text())
        self.assertEqual(doc["fatal"], {"code": "ZIP_UNREADABLE"})
        diff = json.loads((self.feed_dir() / "diffs" / "v0.14.0__auto" / f"{uid(1)}__{uid(3)}.json").read_text())
        self.assertEqual(diff["skipped_uids"], [uid(2)])

    def test_analyzer_crash_becomes_fatal_with_message_in_run_only(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(2): b"CRASH", uid(3): b"OK3"})
        result = self.run_it(dl)
        doc = json.loads(generation_path(self.data, ORG, FEED, uid(2), "v0.14.0__auto").read_text())
        self.assertEqual(doc["fatal"], {"code": "ANALYZER_CRASH"})
        item = [i for i in result.items if i["uid"] == uid(2)][0]
        self.assertEqual(item["error_code"], "ANALYZER_CRASH")

    def test_source_changed_is_detected_across_profiles(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(2): b"OK2", uid(3): b"OK3"})
        self.run_it(dl)
        dl2 = FakeDownloader({uid(1): b"OK1", uid(2): b"OK2-changed", uid(3): b"OK3"})
        result = run_analysis(self.data, self.binary, "v3", downloader=dl2)
        self.assertEqual([w["uid"] for w in result.warnings if w["code"] == "SOURCE_CHANGED"], [uid(2)])
        index = json.loads((self.feed_dir() / "feed.json").read_text())
        status = {g["uid"]: g["source_status"] for g in index["generations"]}
        self.assertEqual(status[uid(2)], "SOURCE_CHANGED")

    def test_unpinned_binary_refuses_unmarked_directory(self):
        (self.data / UNPINNED_MARKER).unlink()
        with self.assertRaises(StoreError):
            self.run_it(FakeDownloader({}))

    def test_zip_files_are_removed(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(2): b"OK2", uid(3): b"OK3"})
        self.run_it(dl)
        self.assertEqual(list(self.data.rglob("*.zip")), [])

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
    def test_outputs_match_schemas(self):
        dl = FakeDownloader({uid(1): b"OK1", uid(2): b"FATAL", uid(3): b"OK3"})
        result = self.run_it(dl)
        gv, dv, fv, rv = (validator(n) for n in ("generation.schema.json", "diff.schema.json",
                                                   "feed.schema.json", "run.schema.json"))
        for path in self.feed_dir().rglob("*.json"):
            doc = json.loads(path.read_text())
            v = fv if path.name == "feed.json" else dv if "diffs" in path.parts else gv
            with self.subTest(path=path.name):
                self.assertEqual(errors(v, doc), [])
        self.assertEqual(errors(rv, json.loads((self.data / result.run_file).read_text())), [])


if __name__ == "__main__":
    unittest.main()
