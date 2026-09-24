import copy
import unittest

from gtfs_jp_monitor.canonical import dumps
from gtfs_jp_semantic.report_check import check_report

from .schema_support import FIXTURES, HAVE_JSONSCHEMA, errors, load_json, validator

EXAMPLE = FIXTURES / "semantic" / "report-example.json"


class ReportExampleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doc = load_json(EXAMPLE)

    def test_example_is_canonical_and_consistent(self):
        self.assertEqual(EXAMPLE.read_text(encoding="utf-8"), dumps(self.doc))
        self.assertEqual(check_report(self.doc), [])

    @unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
    def test_example_matches_schema(self):
        self.assertEqual(errors(validator("semantic-report.schema.json"), self.doc), [])


@unittest.skipUnless(HAVE_JSONSCHEMA, "jsonschema (dev dependency) is not installed")
class ReportSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v = validator("semantic-report.schema.json")
        cls.doc = load_json(EXAMPLE)

    def invalid(self, mutate):
        doc = copy.deepcopy(self.doc)
        mutate(doc)
        self.assertNotEqual(errors(self.v, doc), [])

    def test_notes_are_codes_not_text(self):
        self.invalid(lambda d: d["header"]["notes"].append({"code": "FORMAT_DIFFERENCE", "text": "Biçim farkı"}))
        self.invalid(lambda d: d["header"]["notes"].append({"code": "SOMETHING_ELSE"}))

    def test_day_types_and_bands(self):
        self.invalid(lambda d: d["summary"]["trips_by_day_type"].update(monday={"before": 1, "after": 1}))
        self.invalid(lambda d: d["lines"][0]["trips"][0]["bands"].update({"7-9": {"before": 1, "after": 1}}))

    def test_times_are_minutes(self):
        self.invalid(lambda d: d["lines"][0]["timetables"][0]["old"]["trips"][0]["times"].__setitem__(0, "06:40"))
        self.invalid(lambda d: d["lines"][0]["timetables"][0]["old"]["trips"][0]["times"].__setitem__(0, 3000))

    def test_no_run_time(self):
        self.invalid(lambda d: d.update(generated_at="2026-09-24T00:00:00Z"))

    def test_status_enums(self):
        self.invalid(lambda d: d["lines"][0].update(status="moved"))
        self.invalid(lambda d: d["places"][0].update(status="changed"))


class ReportCheckTest(unittest.TestCase):
    def setUp(self):
        self.doc = copy.deepcopy(load_json(EXAMPLE))

    def problems(self, mutate):
        mutate(self.doc)
        return check_report(self.doc)

    def test_place_references(self):
        self.assertTrue(self.problems(lambda d: d["lines"][0]["patterns"][0]["edits"][0]["places"].append(99)))

    def test_times_match_places(self):
        self.assertTrue(self.problems(lambda d: d["lines"][0]["timetables"][0]["new"]["trips"][0]["times"].pop()))

    def test_pairs_cover_every_trip_once(self):
        self.assertTrue(self.problems(lambda d: d["lines"][0]["timetables"][0]["pairs"].pop()))
        self.doc = copy.deepcopy(load_json(EXAMPLE))
        self.assertTrue(self.problems(lambda d: d["lines"][0]["timetables"][0]["pairs"].append([0, None])))
        self.doc = copy.deepcopy(load_json(EXAMPLE))
        self.assertTrue(self.problems(lambda d: d["lines"][0]["timetables"][0]["pairs"].append([None, None])))

    def test_line_references(self):
        self.assertTrue(self.problems(lambda d: d["places"][0]["lines"].append("99")))
        self.doc = copy.deepcopy(load_json(EXAMPLE))
        self.assertTrue(self.problems(lambda d: d["lines"][1]["related"].append("nope")))

    def test_status_sides(self):
        self.assertTrue(self.problems(lambda d: d["places"][3].update(new=d["places"][0]["new"])))
        self.doc = copy.deepcopy(load_json(EXAMPLE))
        self.assertTrue(self.problems(lambda d: d["lines"][2].update(old={"names": ["3"], "route_ids": ["R3"]})))
        self.doc = copy.deepcopy(load_json(EXAMPLE))
        self.assertTrue(self.problems(lambda d: d["places"][2].update(moved_m=None)))

    def test_move_references(self):
        move = {"day_type": "weekday", "kind": "rerouted", "edits": [],
                "old": {"line": "1", "direction": "0", "trip": 1}, "new": {"line": "1", "direction": "0", "trip": 0}}
        # old trip 1 is removed (unpaired) but new trip 0 is paired with old trip 0
        self.assertEqual(self.problems(lambda d: d["moves"].append(move)), ["moves[0].new: trip is paired in its own line"])
        self.doc = copy.deepcopy(load_json(EXAMPLE))
        self.assertTrue(self.problems(lambda d: d["moves"].append(dict(move, old={"line": "9", "direction": "0", "trip": 0}))))

    def test_coverage_adds_up(self):
        self.assertTrue(self.problems(lambda d: d["header"]["coverage"].update(explained=111)))


if __name__ == "__main__":
    unittest.main()
