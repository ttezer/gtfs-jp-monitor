import json
import tempfile
import unittest
from pathlib import Path

from gtfs_jp_monitor.canonical import delta1, dumps, round1, write_json, yyyymmdd_to_iso


class Round1Test(unittest.TestCase):
    def test_halves_round_away_from_zero(self):
        self.assertEqual(round1(2.25), 2.3)
        self.assertEqual(round1(0.05), 0.1)
        self.assertEqual(round1(-1.25), -1.3)

    def test_long_floats_from_analyzer(self):
        self.assertEqual(round1(199.96164383561643), 200.0)
        self.assertEqual(round1(93.4), 93.4)

    def test_integers_become_floats(self):
        self.assertEqual(round1(100), 100.0)
        self.assertIsInstance(round1(100), float)

    def test_rejects_non_numbers_and_non_finite(self):
        with self.assertRaises(TypeError):
            round1(True)
        with self.assertRaises(TypeError):
            round1("1.0")
        with self.assertRaises(ValueError):
            round1(float("nan"))
        with self.assertRaises(ValueError):
            round1(float("inf"))


class Delta1Test(unittest.TestCase):
    def test_exact_difference(self):
        # Plain float subtraction gives 1.8999999999999915.
        self.assertEqual(delta1(96.2, 98.1), 1.9)

    def test_negative_and_zero(self):
        self.assertEqual(delta1(98.1, 96.2), -1.9)
        self.assertEqual(delta1(96.2, 96.2), 0.0)


class DateTest(unittest.TestCase):
    def test_int_and_str(self):
        self.assertEqual(yyyymmdd_to_iso(20260401), "2026-04-01")
        self.assertEqual(yyyymmdd_to_iso("20270331"), "2027-03-31")

    def test_invalid(self):
        for bad in (2026041, "2026-04-01", 20261301, 20260230):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                yyyymmdd_to_iso(bad)


class DumpsTest(unittest.TestCase):
    def test_sorted_keys_indent_and_trailing_newline(self):
        text = dumps({"b": 1, "a": {"d": 2, "c": 3}})
        self.assertEqual(text, '{\n  "a": {\n    "c": 3,\n    "d": 2\n  },\n  "b": 1\n}\n')

    def test_japanese_is_not_escaped(self):
        self.assertIn("永井運輸", dumps({"name": "永井運輸"}))

    def test_same_content_same_bytes(self):
        self.assertEqual(dumps({"x": 1, "y": [1, 2]}), dumps({"y": [1, 2], "x": 1}))

    def test_nan_is_rejected(self):
        with self.assertRaises(ValueError):
            dumps({"score": float("nan")})


class WriteJsonTest(unittest.TestCase):
    def test_writes_canonical_bytes_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "out.json"
            write_json(target, {"b": "永井", "a": 1.5})
            raw = target.read_bytes()
            self.assertEqual(raw, dumps({"a": 1.5, "b": "永井"}).encode("utf-8"))
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(json.loads(raw), {"a": 1.5, "b": "永井"})
            self.assertEqual([p.name for p in target.parent.iterdir()], ["out.json"])

    def test_failed_serialization_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.json"
            write_json(target, {"ok": True})
            with self.assertRaises(ValueError):
                write_json(target, {"bad": float("nan")})
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"ok": True})


if __name__ == "__main__":
    unittest.main()
