import copy
import unittest

from gtfs_jp_monitor.canonical import dumps

from .schema_support import FIXTURES, HAVE_JSONSCHEMA, errors, load_json, validator


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
class FeedSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = validator("feed.schema.json")
        cls.path = FIXTURES / "feed" / "example.json"
        cls.example = load_json(cls.path)

    def test_example_is_valid_and_canonical(self):
        self.assertEqual(errors(self.v, self.example), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), dumps(self.example))

    def test_rid_is_rejected(self):
        doc = copy.deepcopy(self.example)
        doc["generations"][0]["rid"] = "prev_1"
        self.assertNotEqual(errors(self.v, doc), [])

    def test_unknown_license_is_null_not_free_text(self):
        doc = copy.deepcopy(self.example)
        doc["license"] = {"raw": "CC-BY", "id": None}
        self.assertEqual(errors(self.v, doc), [])
        doc["license"]["id"] = "CC-BY"
        self.assertNotEqual(errors(self.v, doc), [])

    def test_source_status_enum(self):
        doc = copy.deepcopy(self.example)
        doc["generations"][0]["source_status"] = "GONE"
        self.assertNotEqual(errors(self.v, doc), [])

    def test_missing_from_date_is_representable(self):
        doc = copy.deepcopy(self.example)
        doc["generations"][1]["from_date"] = None
        doc["generations"][1]["source_status"] = "ORDERING_UNKNOWN"
        self.assertEqual(errors(self.v, doc), [])


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
class RunSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = validator("run.schema.json")
        cls.path = FIXTURES / "run" / "example.json"
        cls.example = load_json(cls.path)

    def test_example_is_valid_and_canonical(self):
        self.assertEqual(errors(self.v, self.example), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), dumps(self.example))

    def test_rid_values(self):
        for rid, ok in (("current", True), ("prev_12", True), ("next_2", True), ("latest", False), ("prev_", False)):
            with self.subTest(rid=rid):
                doc = copy.deepcopy(self.example)
                doc["items"][0]["rid_observed"] = rid
                self.assertEqual(errors(self.v, doc) == [], ok)

    def test_redirect_url_field_is_rejected(self):
        # Signed S3 redirect URLs carry temporary credentials and must never be recorded.
        doc = copy.deepcopy(self.example)
        doc["items"][0]["download_url"] = "https://example.s3.amazonaws.com/feed.zip?X-Amz-Signature=..."
        self.assertNotEqual(errors(self.v, doc), [])

    def test_message_length_is_bounded(self):
        doc = copy.deepcopy(self.example)
        doc["items"][0]["message"] = "x" * 2001
        self.assertNotEqual(errors(self.v, doc), [])


if __name__ == "__main__":
    unittest.main()
