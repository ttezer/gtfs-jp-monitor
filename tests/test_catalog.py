import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from gtfs_jp_monitor.catalog import sync_catalog
from gtfs_jp_monitor.gtfsdatajp import ApiError, FeedGenerations, FeedRecord, GenerationRecord

from .schema_support import HAVE_JSONSCHEMA, errors, validator

ORG, FEED = "nagai-unyu", "Nagaibus"


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def feed_record(org=ORG, feed=FEED, license_raw="CC BY 4.0") -> FeedRecord:
    return FeedRecord(org, feed, "永井運輸バス", "永井運輸", 10, license_raw, None, False, None, None, None, None, "")


def gen(n: int, rid: str, from_date: str, published_at: str | None = None) -> GenerationRecord:
    return GenerationRecord(
        uid=uid(n), rid=rid,
        gtfs_url=f"https://api.gtfs-data.jp/v2/organizations/{ORG}/feeds/{FEED}/files/feed.zip?uid={uid(n)}",
        from_date=from_date, to_date="2027-03-31", published_at=published_at or f"{from_date}T00:00:00+09:00", memo="",
    )


class FakeClient:
    def __init__(self, feeds, generations, failing=()):
        self.feeds = feeds
        self.generations = generations  # {(org, feed): [GenerationRecord]}
        self.failing = set(failing)

    def list_feeds(self):
        return list(self.feeds), []

    def get_generations(self, org_id, feed_id):
        if (org_id, feed_id) in self.failing:
            raise ApiError("HTTP 503")
        gens = tuple(self.generations.get((org_id, feed_id), ()))
        prev = sum(g.rid.startswith("prev_") for g in gens)
        nxt = sum(g.rid.startswith("next_") for g in gens)
        return FeedGenerations(org_id, feed_id, "CC BY 4.0", prev, nxt, gens)


def read(data_dir: Path, name: str) -> dict:
    if name == "generations.json":  # all per-feed files merged, in path order
        gens = []
        for path in sorted((data_dir / "catalog" / "generations").rglob("*.json")):
            gens.extend(json.loads(path.read_text(encoding="utf-8"))["generations"])
        return {"schema": "gtfs-jp-monitor-catalog-generations/1", "generations": gens}
    return json.loads((data_dir / "catalog" / name).read_text(encoding="utf-8"))


BASE_GENS = [gen(3, "current", "2026-04-01"), gen(2, "prev_1", "2025-10-01"), gen(1, "prev_2", "2025-04-01")]


class CatalogSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def sync(self, client, **kwargs):
        return sync_catalog(client, self.data, **kwargs)

    def test_first_sync_writes_ordered_catalog(self):
        result = self.sync(FakeClient([feed_record()], {(ORG, FEED): BASE_GENS}))
        self.assertEqual(sorted(result.changed_files), ["catalog/feeds.json", f"catalog/generations/{ORG}/{FEED}.json"])
        self.assertEqual(len(result.new_generations), 3)
        uids = [g["uid"] for g in read(self.data, "generations.json")["generations"]]
        self.assertEqual(uids, [uid(1), uid(2), uid(3)])  # oldest first

    def test_unchanged_source_changes_nothing(self):
        # data-model §7: a second run on an unchanged catalog finds nothing new.
        client = FakeClient([feed_record()], {(ORG, FEED): BASE_GENS})
        self.sync(client)
        before = {p: p.read_bytes() for p in (self.data / "catalog").rglob("*.json")}
        result = self.sync(client)
        self.assertEqual(result.changed_files, [])
        self.assertEqual(result.new_generations, [])
        self.assertEqual({p: p.read_bytes() for p in (self.data / "catalog").rglob("*.json")}, before)

    def test_new_generation_rewrites_only_its_feed_file(self):
        other = feed_record(org="other-org", feed="OtherFeed")
        self.sync(FakeClient([feed_record(), other], {(ORG, FEED): BASE_GENS}))
        result = self.sync(FakeClient([feed_record(), other],
                                      {(ORG, FEED): BASE_GENS + [gen(4, "next_1", "2026-10-01")]}))
        self.assertEqual(result.changed_files, [f"catalog/generations/{ORG}/{FEED}.json"])

    def test_rid_shift_is_not_a_new_generation(self):
        self.sync(FakeClient([feed_record()], {(ORG, FEED): BASE_GENS}))
        shifted = [replace(BASE_GENS[0], rid="prev_1"), replace(BASE_GENS[1], rid="prev_2"), replace(BASE_GENS[2], rid="prev_3"),
                   gen(4, "current", "2026-10-01")]
        result = self.sync(FakeClient([feed_record()], {(ORG, FEED): shifted}))
        self.assertEqual(result.new_generations, [(ORG, FEED, uid(4))])

    def test_next_becoming_current_is_not_new(self):
        with_next = BASE_GENS + [gen(4, "next_1", "2026-10-01")]
        self.sync(FakeClient([feed_record()], {(ORG, FEED): with_next}))
        promoted = [replace(g, rid=r) for g, r in zip(with_next, ["prev_1", "prev_2", "prev_3", "current"])]
        result = self.sync(FakeClient([feed_record()], {(ORG, FEED): promoted}))
        self.assertEqual(result.new_generations, [])

    def test_failed_feed_keeps_previous_entries(self):
        self.sync(FakeClient([feed_record()], {(ORG, FEED): BASE_GENS}))
        before = read(self.data, "generations.json")
        result = self.sync(FakeClient([feed_record()], {}, failing=[(ORG, FEED)]))
        self.assertEqual([w["code"] for w in result.warnings], ["FEED_FETCH_FAILED"])
        self.assertEqual(read(self.data, "generations.json"), before)

    def test_disappeared_generation_is_kept_and_warned_once(self):
        self.sync(FakeClient([feed_record()], {(ORG, FEED): BASE_GENS}))
        client = FakeClient([feed_record()], {(ORG, FEED): BASE_GENS[:2]})
        first = self.sync(client)
        self.assertEqual([w["code"] for w in first.warnings], ["GENERATION_DISAPPEARED"])
        gone = [g for g in read(self.data, "generations.json")["generations"] if g["uid"] == uid(1)][0]
        self.assertEqual((gone["present"], gone["rid_observed"]), (False, None))
        second = self.sync(client)
        self.assertEqual(second.warnings, [])

    def test_unlisted_feed_is_kept(self):
        other = feed_record(org="other-org", feed="OtherFeed")
        self.sync(FakeClient([feed_record(), other], {(ORG, FEED): BASE_GENS}))
        result = self.sync(FakeClient([feed_record()], {(ORG, FEED): BASE_GENS}))
        self.assertEqual([w["code"] for w in result.warnings], ["FEED_UNLISTED"])
        feeds = {f["feed_id"]: f for f in read(self.data, "feeds.json")["feeds"]}
        self.assertFalse(feeds["OtherFeed"]["listed"])
        self.assertTrue(feeds[FEED]["listed"])

    def test_only_filter_leaves_other_feeds_untouched(self):
        other = feed_record(org="other-org", feed="OtherFeed")
        self.sync(FakeClient([feed_record(), other], {(ORG, FEED): BASE_GENS}))
        self.sync(FakeClient([feed_record(), other], {(ORG, FEED): [gen(9, "current", "2027-04-01")]}),
                  only={("other-org", "OtherFeed")})
        uids = [g["uid"] for g in read(self.data, "generations.json")["generations"] if g["feed_id"] == FEED]
        self.assertEqual(uids, [uid(1), uid(2), uid(3)])

    def test_unknown_license_and_rid_mismatch_are_warned(self):
        wrong_rids = [replace(BASE_GENS[0], rid="prev_1"), replace(BASE_GENS[1], rid="current"), BASE_GENS[2]]
        result = self.sync(FakeClient([feed_record(license_raw="CC-BY")], {(ORG, FEED): wrong_rids}))
        self.assertEqual(sorted(w["code"] for w in result.warnings), ["RID_ORDER_MISMATCH", "UNKNOWN_LICENSE"])
        self.assertIsNone(read(self.data, "feeds.json")["feeds"][0]["license"]["id"])

    def test_lasting_conditions_become_notes_not_events(self):
        wrong_rids = [replace(BASE_GENS[0], rid="prev_1"), replace(BASE_GENS[1], rid="current"), BASE_GENS[2]]
        client = FakeClient([feed_record(license_raw="CC-BY")], {(ORG, FEED): wrong_rids})
        first = self.sync(client)
        self.assertEqual(first.events, [])
        self.assertEqual(read(self.data, "feeds.json")["feeds"][0]["notes"], ["RID_ORDER_MISMATCH", "UNKNOWN_LICENSE"])
        second = self.sync(client)
        self.assertEqual(second.changed_files, [])  # a repeating condition does not rewrite anything
        fixed = self.sync(FakeClient([feed_record()], {(ORG, FEED): BASE_GENS}))
        self.assertIn("catalog/feeds.json", fixed.changed_files)
        self.assertEqual(read(self.data, "feeds.json")["feeds"][0]["notes"], [])

    def test_events_and_kept_notes_on_failure(self):
        self.sync(FakeClient([feed_record(license_raw="CC-BY")], {(ORG, FEED): BASE_GENS}))
        failed = self.sync(FakeClient([feed_record(license_raw="CC-BY")], {}, failing=[(ORG, FEED)]))
        self.assertEqual([e["code"] for e in failed.events], ["FEED_FETCH_FAILED"])
        self.assertEqual(read(self.data, "feeds.json")["feeds"][0]["notes"], ["UNKNOWN_LICENSE"])

    def test_rejected_list_entries_are_recorded_in_the_catalog(self):
        class Client(FakeClient):
            def list_feeds(self):
                from gtfs_jp_monitor.gtfsdatajp import Rejected
                return list(self.feeds), [Rejected("INVALID_FEED_ID", "../x", "y", None, "bad")]
        self.sync(Client([feed_record()], {(ORG, FEED): BASE_GENS}))
        self.assertEqual(read(self.data, "feeds.json")["rejected"], [{"code": "INVALID_FEED_ID", "feed_id": "y", "org_id": "../x"}])

    def test_missing_from_date_is_listed_last(self):
        gens = BASE_GENS + [replace(gen(0, "prev_3", "2025-01-01"), from_date=None)]
        result = self.sync(FakeClient([feed_record()], {(ORG, FEED): gens}))
        self.assertIn("ORDERING_UNKNOWN", [w["code"] for w in result.warnings])
        self.assertEqual(read(self.data, "generations.json")["generations"][-1]["uid"], uid(0))

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
    def test_outputs_match_schemas(self):
        self.sync(FakeClient([feed_record(), feed_record(org="x-org", feed="X", license_raw="CC-BY")],
                             {(ORG, FEED): BASE_GENS + [gen(4, "next_1", "2026-10-01")]}))
        self.assertEqual(errors(validator("catalog-feeds.schema.json"), read(self.data, "feeds.json")), [])
        self.assertEqual(errors(validator("catalog-generations.schema.json"), read(self.data, "generations.json")), [])


if __name__ == "__main__":
    unittest.main()
