import copy
import unittest

from gtfs_jp_monitor.canonical import dumps

from .schema_support import FIXTURES, HAVE_JSONSCHEMA, errors, load_json, validator

FIXTURE_DIR = FIXTURES / "generation"


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
class GenerationSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = validator("generation.schema.json")
        cls.complete = load_json(FIXTURE_DIR / "complete.json")

    def assertValid(self, doc):
        self.assertEqual(errors(self.v, doc), [])

    def assertInvalid(self, doc):
        self.assertNotEqual(errors(self.v, doc), [])

    def test_fixtures_are_valid(self):
        for name in ("complete.json", "partial.json", "fatal.json"):
            with self.subTest(name=name):
                self.assertValid(load_json(FIXTURE_DIR / name))

    def test_fixtures_are_canonical(self):
        for name in ("complete.json", "partial.json", "fatal.json"):
            with self.subTest(name=name):
                raw = (FIXTURE_DIR / name).read_text(encoding="utf-8")
                self.assertEqual(raw, dumps(load_json(FIXTURE_DIR / name)))

    def test_rid_and_run_time_are_rejected(self):
        # rid and timestamps change over time; they must never enter a generation record.
        for field in ("rid", "rid_observed", "generated_at", "duration_ms"):
            with self.subTest(field=field):
                doc = copy.deepcopy(self.complete)
                doc["generation"][field] = "current"
                self.assertInvalid(doc)
                top = copy.deepcopy(self.complete)
                top[field] = "x"
                self.assertInvalid(top)

    def test_human_text_fields_are_rejected(self):
        doc = copy.deepcopy(self.complete)
        doc["rules"]["JPN_030"]["title"] = "Platform code missing"
        self.assertInvalid(doc)

    def test_fatal_must_not_carry_scores(self):
        doc = load_json(FIXTURE_DIR / "fatal.json")
        doc["scores"] = copy.deepcopy(self.complete["scores"])
        self.assertInvalid(doc)

    def test_fatal_requires_code(self):
        doc = load_json(FIXTURE_DIR / "fatal.json")
        doc["fatal"] = None
        self.assertInvalid(doc)

    def test_complete_must_not_have_partial_details(self):
        doc = copy.deepcopy(self.complete)
        doc["partial"] = load_json(FIXTURE_DIR / "partial.json")["partial"]
        self.assertInvalid(doc)

    def test_partial_requires_details(self):
        doc = load_json(FIXTURE_DIR / "partial.json")
        doc["partial"] = None
        self.assertInvalid(doc)

    def test_identifier_patterns(self):
        cases = [
            ("generation", "uid", "17AB34E1-DCB8-4B2E-9CDA-AE2B68F4C444"),
            ("generation", "uid", "not-a-uid"),
            ("feed", "org_id", "../etc"),
            ("feed", "feed_id", "a/b"),
            ("generation", "from_date", "20260401"),
            ("generation", "sha256", "ABC"),
        ]
        for section, key, bad in cases:
            with self.subTest(key=key, bad=bad):
                doc = copy.deepcopy(self.complete)
                doc[section][key] = bad
                self.assertInvalid(doc)

    def test_rule_ids_and_enums(self):
        for rule_id in ("DQ_005b", "JPN_030", "ARC_001"):
            with self.subTest(rule_id=rule_id):
                doc = copy.deepcopy(self.complete)
                doc["rules"] = {rule_id: {"count": 1, "severity": "INFO", "class": "SPEC"}}
                self.assertValid(doc)
        for bad in ({"jpn_030": {"count": 1, "severity": "INFO", "class": "SPEC"}},
                    {"JPN_030": {"count": 0, "severity": "INFO", "class": "SPEC"}},
                    {"JPN_030": {"count": 1, "severity": "WARNING", "class": "SPEC"}}):
            with self.subTest(bad=bad):
                doc = copy.deepcopy(self.complete)
                doc["rules"] = bad
                self.assertInvalid(doc)

    def test_analyzer_identity_is_required(self):
        for key in ("binary_sha256", "platform", "gtfs_jp_profile", "validate_date"):
            with self.subTest(key=key):
                doc = copy.deepcopy(self.complete)
                del doc["analyzer"][key]
                self.assertInvalid(doc)

    def test_validate_date_source_is_fixed(self):
        doc = copy.deepcopy(self.complete)
        doc["analyzer"]["validate_date_source"] = "published_at"
        self.assertInvalid(doc)

    def test_missing_feed_info_range_is_allowed(self):
        doc = copy.deepcopy(self.complete)
        doc["metrics"]["feed_validity"] = None
        self.assertValid(doc)


if __name__ == "__main__":
    unittest.main()
