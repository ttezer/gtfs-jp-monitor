import tempfile
import unittest
from pathlib import Path

from gtfs_jp_monitor.canonical import write_json
from gtfs_jp_monitor.ids import InvalidIdentifier
from gtfs_jp_monitor.store import (
    UNPINNED_MARKER,
    StoreError,
    analysis_key,
    build_feed_index,
    check_writable,
    diff_path,
    generation_path,
    list_analyses,
    split_key,
)

from .schema_support import HAVE_JSONSCHEMA, errors, validator

ORG, FEED = "nagai-unyu", "Nagaibus"


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


CATALOG_FEED = {
    "org_id": ORG, "feed_id": FEED, "feed_name": "永井運輸バス", "organization_name": "永井運輸", "feed_pref_id": 10,
    "license": {"raw": "CC BY 4.0", "id": "CC-BY-4.0"}, "license_url": None, "is_discontinued": False,
    "discontinued_date": None, "latest_feed_start_date": None, "latest_feed_end_date": None, "memo": "", "listed": True,
}


def cat_gen(n, from_date="2026-04-01", present=True):
    return {"org_id": ORG, "feed_id": FEED, "uid": uid(n), "rid_observed": "current" if present else None,
            "gtfs_url": "https://api.gtfs-data.jp/x", "from_date": from_date, "to_date": "2027-03-31",
            "published_at": "2026-03-01T00:00:00+09:00", "memo": "", "present": present}


class KeysAndPathsTest(unittest.TestCase):
    def test_analysis_key(self):
        self.assertEqual(analysis_key("v0.14.0", "auto"), "v0.14.0__auto")
        self.assertEqual(split_key("v0.14.0__v4"), ("v0.14.0", "v4"))
        for release, profile in (("0.14.0", "auto"), ("v0.14.0", "v5"), ("v0.14", "auto"), ("v0.14.0/..", "auto")):
            with self.subTest(release=release, profile=profile), self.assertRaises(StoreError):
                analysis_key(release, profile)

    def test_paths(self):
        root = Path("/data")
        self.assertEqual(
            generation_path(root, ORG, FEED, uid(1), "v0.14.0__auto"),
            root / "feeds" / ORG / FEED / "generations" / uid(1) / "v0.14.0__auto.json",
        )
        self.assertEqual(
            diff_path(root, ORG, FEED, "v0.14.0__auto", uid(1), uid(2)),
            root / "feeds" / ORG / FEED / "diffs" / "v0.14.0__auto" / f"{uid(1)}__{uid(2)}.json",
        )
        with self.assertRaises(InvalidIdentifier):
            generation_path(root, ORG, FEED, "../x", "v0.14.0__auto")

    def test_unpinned_binaries_need_a_marked_scratch_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            check_writable(Path(tmp), pinned=True)
            with self.assertRaises(StoreError):
                check_writable(Path(tmp), pinned=False)
            (Path(tmp) / UNPINNED_MARKER).write_text("")
            check_writable(Path(tmp), pinned=False)


class FeedIndexTest(unittest.TestCase):
    def test_list_analyses_ignores_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(generation_path(root, ORG, FEED, uid(1), "v0.14.0__auto"), {"validation_status": "COMPLETE"})
            write_json(generation_path(root, ORG, FEED, uid(1), "v0.14.0__v3"), {"validation_status": "PARTIAL"})
            stray = root / "feeds" / ORG / FEED / "generations" / "not-a-uid"
            stray.mkdir(parents=True)
            (stray / "v0.14.0__auto.json").write_text("{}")
            (generation_path(root, ORG, FEED, uid(1), "v0.14.0__auto").parent / "notes.json").write_text("{}")
            self.assertEqual(list_analyses(root, ORG, FEED), {uid(1): {"v0.14.0__auto": "COMPLETE", "v0.14.0__v3": "PARTIAL"}})

    def test_statuses_and_canonical(self):
        gens = [cat_gen(1, "2025-10-01"), cat_gen(2, "2026-04-01"), cat_gen(3, None), cat_gen(4, "2024-01-01", present=False)]
        analyses = {uid(1): {"v0.14.0__auto": "COMPLETE"}, uid(2): {"v0.14.0__auto": "FATAL"}}
        doc = build_feed_index(CATALOG_FEED, gens, analyses, "v0.14.0__auto", source_changed={uid(2)})
        by_uid = {g["uid"]: g for g in doc["generations"]}
        self.assertEqual([g["uid"] for g in doc["generations"]], [uid(1), uid(2), uid(3), uid(4)])  # catalog order kept
        self.assertEqual(by_uid[uid(1)]["source_status"], "AVAILABLE")
        self.assertEqual(by_uid[uid(2)]["source_status"], "SOURCE_CHANGED")
        self.assertEqual(by_uid[uid(3)]["source_status"], "ORDERING_UNKNOWN")
        self.assertEqual(by_uid[uid(4)]["source_status"], "SOURCE_UNAVAILABLE")
        self.assertEqual(by_uid[uid(1)]["canonical"], {"release_tag": "v0.14.0", "gtfs_jp_profile": "auto"})
        self.assertIsNone(by_uid[uid(3)]["canonical"])

    def test_previous_canonical_and_source_changed_are_kept(self):
        first = build_feed_index(CATALOG_FEED, [cat_gen(1)], {uid(1): {"v0.14.0__auto": "COMPLETE"}}, "v0.14.0__auto",
                                 source_changed={uid(1)})
        # A later run with a new release that has not re-analysed uid(1) yet.
        second = build_feed_index(CATALOG_FEED, [cat_gen(1)], {uid(1): {"v0.14.0__auto": "COMPLETE"}}, "v0.15.0__auto",
                                  previous=first)
        entry = second["generations"][0]
        self.assertEqual(entry["canonical"], {"release_tag": "v0.14.0", "gtfs_jp_profile": "auto"})
        self.assertEqual(entry["source_status"], "SOURCE_CHANGED")

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
    def test_feed_index_matches_schema(self):
        gens = [cat_gen(1, "2025-10-01"), cat_gen(3, None), cat_gen(4, "2024-01-01", present=False)]
        doc = build_feed_index(dict(CATALOG_FEED, discontinued_date="2026-03-31", is_discontinued=True), gens,
                               {uid(1): {"v0.14.0__auto": "COMPLETE", "v0.14.0__v4": "PARTIAL"}}, "v0.14.0__auto")
        self.assertEqual(errors(validator("feed.schema.json"), doc), [])


if __name__ == "__main__":
    unittest.main()
