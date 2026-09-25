import unittest
from datetime import date

from gtfs_jp_semantic.holidays import HolidayTableOutOfRange, default_table


class HolidayTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.table = default_table()

    def test_version_and_range(self):
        self.assertTrue(self.table.version.startswith("cao-"))
        self.assertLessEqual(self.table.first_year, 2018)
        self.assertGreaterEqual(self.table.last_year, 2027)

    def test_known_holidays(self):
        for d in ("2026-01-01", "2026-01-12", "2025-05-06", "2019-05-01", "2020-07-24", "2021-07-23", "2027-11-23"):
            with self.subTest(d=d):
                self.assertTrue(self.table.is_holiday(date.fromisoformat(d)))

    def test_ordinary_days(self):
        for d in ("2026-01-05", "2020-10-12", "2021-10-11"):  # sports day moved in 2020 and 2021
            with self.subTest(d=d):
                self.assertFalse(self.table.is_holiday(date.fromisoformat(d)))

    def test_categories(self):
        cases = {
            "2026-04-01": "wed",
            "2026-04-04": "sat",
            "2026-04-05": "sun",
            "2026-04-29": "hol",  # Showa Day on a Wednesday
            "2026-01-12": "hol",  # Coming of Age Day (Monday)
        }
        for d, expected in cases.items():
            with self.subTest(d=d):
                self.assertEqual(self.table.category(date.fromisoformat(d)), expected)

    def test_uncovered_years_raise(self):
        with self.assertRaises(HolidayTableOutOfRange):
            self.table.category(date(self.table.last_year + 1, 1, 5))


if __name__ == "__main__":
    unittest.main()
