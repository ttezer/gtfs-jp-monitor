import copy
import json
import re
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

from gtfs_jp_monitor.atom import LABELS, build_atom
from gtfs_jp_monitor.classify import load_rules
from gtfs_jp_monitor.impact import assess

from .test_semantic_events import build
from .test_semantic_report import NEW, OLD

PAGE = Path(__file__).resolve().parent.parent / "web" / "prototype" / "index.html"
NS = {"a": "http://www.w3.org/2005/Atom"}


def js_object(text: str) -> dict:
    return json.loads(re.sub(r"(\w+): ", r'"\1": ', text))


class LabelsTest(unittest.TestCase):
    def test_same_labels_as_the_page(self):
        html = PAGE.read_text(encoding="utf-8")
        short = re.findall(r"evtShort: (\{[^}]*\})", html)
        imp = re.findall(r"\n    imp: (\{[^}]*\})", html)
        for lang, s, i in zip(("tr", "en", "fr", "ja"), short, imp, strict=True):
            with self.subTest(lang=lang):
                self.assertEqual(list(js_object(s).items()), list(LABELS[lang]["evt"].items()))  # order too
                self.assertEqual(js_object(i), LABELS[lang]["imp"])


class AtomTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        report, _ = build(OLD, NEW)
        report["impact"] = assess(report, load_rules())
        assert report["impact"]["class"] == "major"
        org, feed = report["header"]["feed"]["org_id"], report["header"]["feed"]["feed_id"]
        later = copy.deepcopy(report)
        later["header"]["old"]["uid"], later["header"]["new"]["uid"] = report["header"]["new"]["uid"], "3eb242c1-7d2e-4f0e-9b1a-5c6d7e8f9a0b"
        later["header"]["new"]["from_date"], later["header"]["new"]["published_at"] = "2026-07-01", "2026-06-20T10:00:00+09:00"
        minor = copy.deepcopy(report)
        minor["header"]["feed"]["feed_id"] = "Quiet"
        minor["impact"]["class"] = "minor"
        cls.files = {f"{org}/{feed}/a": report, f"{org}/{feed}/b": later, f"{org}/Quiet/c": minor}
        cls.feeds = [{"org_id": org, "feed_id": feed, "name": "サンプルバス"}, {"org_id": org, "feed_id": "Quiet", "name": "静か"}]
        cls.out = build_atom(cls.feeds, cls.files, "https://example.jp/m/")
        cls.org, cls.feed = org, feed

    def parse(self, path):
        return ET.fromstring(self.out[path])

    def test_files(self):
        self.assertEqual(sorted(self.out), [f"atom/{lang}/{name}" for lang in ("en", "ja", "tr")
                                            for name in ("all.xml", f"{self.org}/{self.feed}.xml")])  # no file for a quiet feed

    def test_entries_newest_first_major_only(self):
        root = self.parse(f"atom/en/{self.org}/{self.feed}.xml")
        self.assertEqual(root.get("{http://www.w3.org/XML/1998/namespace}lang"), "en")
        entries = root.findall("a:entry", NS)
        self.assertEqual([e.find("a:updated", NS).text for e in entries], ["2026-06-20T10:00:00+09:00", "2026-04-01T00:00:00+09:00"])
        self.assertEqual(root.find("a:updated", NS).text, "2026-06-20T10:00:00+09:00")
        first = entries[0]
        self.assertEqual(first.find("a:title", NS).text, "サンプルバス · 2026-07-01 · Major impact")
        self.assertEqual(first.find("a:link", NS).get("href"),
                         f"https://example.jp/m/#feed={self.org}/{self.feed}&old={self.files[f'{self.org}/{self.feed}/a']['header']['new']['uid']}"
                         "&new=3eb242c1-7d2e-4f0e-9b1a-5c6d7e8f9a0b&tab=reports")
        content = first.find("a:content", NS).text.splitlines()
        # major lines first, labels by importance
        self.assertEqual(content[:3], ["2: stop skipped · stop inserted", "3: new line · more trips", "1: trips moved to another line · first/last trip changed · retimed"])
        self.assertIn("Stops: stop added ×1 · stop removed ×1 · stop renamed ×1", content)

    def test_another_site(self):
        out = build_atom(self.feeds, self.files, "https://example.fr/m/", langs=["en"], titles={"en": "GTFS-FR Monitor · major changes"},
                         author="GTFS-FR Monitor", tag="gtfs-fr-monitor")
        self.assertEqual(sorted(out), ["atom/en/all.xml", f"atom/en/{self.org}/{self.feed}.xml"])
        root = ET.fromstring(out["atom/en/all.xml"])
        self.assertEqual((root.find("a:title", NS).text, root.find("a:author/a:name", NS).text),
                         ("GTFS-FR Monitor · major changes", "GTFS-FR Monitor"))
        self.assertIn(":gtfs-fr-monitor/", root.find("a:entry/a:id", NS).text)

    def test_deterministic(self):
        self.assertEqual(build_atom(self.feeds, self.files, "https://example.jp/m/"), self.out)


if __name__ == "__main__":
    unittest.main()
