import copy
import unittest

from gtfs_jp_monitor.canonical import dumps

from .schema_support import FIXTURES, HAVE_JSONSCHEMA, errors, load_json, validator

EXAMPLE = FIXTURES / "diff" / "example.json"


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
class DiffSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = validator("diff.schema.json")
        cls.example = load_json(EXAMPLE)

    def assertValid(self, doc):
        self.assertEqual(errors(self.v, doc), [])

    def assertInvalid(self, doc):
        self.assertNotEqual(errors(self.v, doc), [])

    def test_example_is_valid_and_canonical(self):
        self.assertValid(self.example)
        self.assertEqual(EXAMPLE.read_text(encoding="utf-8"), dumps(self.example))

    def test_rule_buckets_keep_counts(self):
        # An id-only list would lose the counts the web table shows.
        doc = copy.deepcopy(self.example)
        doc["rules"]["fixed"] = ["CAL_013"]
        self.assertInvalid(doc)

    def test_all_rule_buckets_are_required(self):
        for bucket in ("fixed", "new", "increased", "decreased", "same"):
            with self.subTest(bucket=bucket):
                doc = copy.deepcopy(self.example)
                del doc["rules"][bucket]
                self.assertInvalid(doc)

    def test_analyzer_identity_is_required(self):
        for key in ("binary_sha256", "platform", "gtfs_jp_profile"):
            with self.subTest(key=key):
                doc = copy.deepcopy(self.example)
                del doc["analyzer"][key]
                self.assertInvalid(doc)

    def test_uids_must_be_real_uids(self):
        doc = copy.deepcopy(self.example)
        doc["old_uid"] = "A"
        self.assertInvalid(doc)

    def test_count_deltas_are_integers(self):
        doc = copy.deepcopy(self.example)
        doc["metrics"]["routes"]["delta"] = 2.5
        self.assertInvalid(doc)

    def test_missing_file_side_is_null(self):
        doc = copy.deepcopy(self.example)
        doc["files"]["shapes.txt"] = {"before": 10, "after": None, "delta": -10}
        self.assertValid(doc)
        doc["files"]["../shapes.txt"] = {"before": 1, "after": 1, "delta": 0}
        self.assertInvalid(doc)

    def test_no_time_dependent_fields(self):
        for field in ("generated_at", "rid", "old_rid"):
            with self.subTest(field=field):
                doc = copy.deepcopy(self.example)
                doc[field] = "x"
                self.assertInvalid(doc)


if __name__ == "__main__":
    unittest.main()
