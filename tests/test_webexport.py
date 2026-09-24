import copy
import tempfile
import unittest
from pathlib import Path

from gtfs_jp_monitor.canonical import write_json
from gtfs_jp_monitor.catalog import sync_catalog
from gtfs_jp_monitor.store import generation_path
from gtfs_jp_monitor.webexport import build_export

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
            export = build_export(root, KEY)
        self.assertEqual(export["schema"], "gtfs-jp-monitor-web-export/1")
        self.assertEqual([f["feed_id"] for f in export["feeds"]], [FEED])  # feeds without analyses are left out
        feed = export["feeds"][0]
        self.assertEqual([g["uid"] for g in feed["generations"]], [uid(1), uid(3)])
        g = feed["generations"][1]
        self.assertEqual(g["rid"], "current")
        self.assertEqual(g["rules"]["JPN_030"], [4, "MEDIUM", "QUALITY"])
        self.assertEqual(export["rule_titles"], {"tr": {}, "en": {}, "ja": {}})


if __name__ == "__main__":
    unittest.main()
