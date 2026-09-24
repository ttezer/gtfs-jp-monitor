import copy
import unittest

from gtfs_jp_monitor.canonical import dumps
from gtfs_jp_monitor.generation import (
    AnalyzerIdentity,
    AnalyzerOutputError,
    FeedMeta,
    GenerationMeta,
    build_fatal,
    build_generation,
    fatal_code,
)
from gtfs_jp_monitor.ids import InvalidIdentifier

from .schema_support import FIXTURES, HAVE_JSONSCHEMA, errors, load_json, validator

FEED = FeedMeta("nagai-unyu", "Nagaibus", "永井運輸バス")
GEN = GenerationMeta(
    uid="17ab34e1-dcb8-4b2e-9cda-ae2b68f4c444",
    sha256="0a1323615c1b2a2157c1cd9667d8a6eeda480a5d97dca8e509093387f04d4ddc",
    published_at="2026-07-08T18:09:26.517446+09:00",
    from_date="2026-04-01",
    to_date="2027-03-31",
    license="CC BY 4.0",
)
ANALYZER = AnalyzerIdentity("0.14.0", "v0.14.0", "1" * 64, "linux", "x86_64", "auto")


def report(name: str) -> dict:
    return load_json(FIXTURES / "analyzer" / name)


class BuildGenerationTest(unittest.TestCase):
    def test_complete_report(self):
        doc = build_generation(report("ok.json"), FEED, GEN, ANALYZER)
        self.assertEqual(doc["validation_status"], "COMPLETE")
        self.assertIsNone(doc["partial"])
        self.assertFalse(doc["publishable"])
        self.assertEqual(
            doc["scores"],
            {"publish": 60.0, "overall": 81.3, "spec": 90.0, "interop": 100.0, "quality": 77.8, "analytics": 100.0},
        )
        self.assertEqual(doc["metrics"]["avg_daily_trips"], 2.0)
        self.assertEqual(doc["metrics"]["service_range"], {"start": "2026-04-01", "end": "2026-04-30"})
        self.assertIsNone(doc["metrics"]["feed_validity"])  # 0 dates = no feed_info
        self.assertEqual(doc["rules"]["JPN_030"], {"count": 2, "severity": "MEDIUM", "class": "QUALITY"})
        self.assertEqual(doc["rules"]["DQ_005b"]["count"], 1)
        self.assertEqual(doc["summary"]["by_severity"], {"CRITICAL": 1, "HIGH": 0, "MEDIUM": 2, "LOW": 1, "INFO": 1})
        self.assertEqual(doc["summary"]["by_class"], {"SPEC": 1, "INTEROP": 0, "QUALITY": 3, "ANALYTICS": 1})
        self.assertEqual(doc["file_row_counts"], {"stops.txt": 3, "trips.txt": 2})

    def test_validate_date_is_from_date(self):
        doc = build_generation(report("ok.json"), FEED, GEN, ANALYZER)
        self.assertEqual((doc["analyzer"]["validate_date"], doc["analyzer"]["validate_date_source"]), ("2026-04-01", "from_date"))

    def test_no_human_text_is_copied(self):
        text = dumps(build_generation(report("partial.json"), FEED, GEN, ANALYZER))
        for phrase in ("Başlık", "İnsan-okunur", "Düzeltme", "alt dizin"):
            self.assertNotIn(phrase, text)

    def test_partial_report(self):
        doc = build_generation(report("partial.json"), FEED, GEN, ANALYZER)
        self.assertEqual(doc["validation_status"], "PARTIAL")
        self.assertEqual(
            doc["partial"],
            {"root_structural_error_count": 1, "unavailable_files": ["calendar.txt", "shapes.txt"],
             "skipped_stages": ["K4"], "skipped_checks": []},
        )
        self.assertFalse(doc["coverage_complete"])

    def test_fatal_report(self):
        doc = build_generation(report("fatal.json"), FEED, GEN, ANALYZER)
        self.assertEqual(doc["validation_status"], "FATAL")
        self.assertEqual(doc["fatal"], {"code": "ZIP_UNREADABLE"})
        self.assertIsNone(doc["scores"])
        self.assertNotIn("okunamadı", dumps(doc))

    def test_monitor_fatal(self):
        doc = build_fatal(FEED, GEN, ANALYZER, "TIMEOUT")
        self.assertEqual(doc["fatal"], {"code": "TIMEOUT"})

    def test_deterministic(self):
        a = dumps(build_generation(report("ok.json"), FEED, GEN, ANALYZER))
        shuffled = report("ok.json")
        shuffled["notices"].reverse()
        shuffled["metrics"]["file_stats"].reverse()
        self.assertEqual(a, dumps(build_generation(shuffled, FEED, GEN, ANALYZER)))

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
    def test_outputs_match_schema(self):
        v = validator("generation.schema.json")
        for name in ("ok.json", "partial.json", "fatal.json"):
            with self.subTest(name=name):
                self.assertEqual(errors(v, build_generation(report(name), FEED, GEN, ANALYZER)), [])

    def test_malformed_reports_are_rejected(self):
        base = report("ok.json")
        mutations = {
            "unknown status": lambda r: r.update(status="weird"),
            "status contradiction": lambda r: r.update(validation_status="PARTIAL"),
            "missing r5": lambda r: r["reports"].pop("r5"),
            "missing publishable": lambda r: r["reports"]["r1"].pop("publishable"),
            "bad severity": lambda r: r["notices"][0].update(severity="WARNING"),
            "bad rule id": lambda r: r["notices"][0].update(rule_id="../x"),
            "inconsistent rule": lambda r: r["notices"][1].update(severity="HIGH"),
            "path in file name": lambda r: r["metrics"]["file_stats"][0].update(name="sub/stops.txt"),
            "negative count": lambda r: r["metrics"].update(trip_count=-1),
            "half-open range": lambda r: r["metrics"].update(service_end_date=0),
            "bad date": lambda r: r["metrics"].update(service_start_date=20261399),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                bad = copy.deepcopy(base)
                mutate(bad)
                with self.assertRaises(AnalyzerOutputError):
                    build_generation(bad, FEED, GEN, ANALYZER)

    def test_identifiers_are_validated(self):
        with self.assertRaises(InvalidIdentifier):
            build_generation(report("ok.json"), FeedMeta("..", "x", ""), GEN, ANALYZER)


class FatalCodeTest(unittest.TestCase):
    def test_conversion(self):
        self.assertEqual(fatal_code("ZipUnreadable"), "ZIP_UNREADABLE")
        self.assertEqual(fatal_code("Utf8Critical"), "UTF8_CRITICAL")
        self.assertEqual(fatal_code("TIMEOUT"), "TIMEOUT")
        self.assertEqual(fatal_code(None), "UNKNOWN_FATAL")
        self.assertEqual(fatal_code("bad code!"), "UNKNOWN_FATAL")


if __name__ == "__main__":
    unittest.main()
