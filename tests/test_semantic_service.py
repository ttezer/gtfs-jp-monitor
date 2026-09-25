import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from gtfs_jp_semantic.holidays import default_table
from gtfs_jp_semantic.reader import Config, read_feed
from gtfs_jp_semantic.service import build_calendar, choose_comparison

CONFIG = Config(100, 1_000_000, 100_000_000, special_max_days=10, min_overlap_days=14, matching=Config.load().matching)
WEEKDAYS = "mon,tue,wed,thu,fri"
HOL = default_table()
CAL_HEADER = "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"


def feed(tmp: Path, name: str, calendar: str, dates: str = "", trips: str = "", feed_info: str = ""):
    path = tmp / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("calendar.txt", CAL_HEADER + calendar)
        if dates:
            zf.writestr("calendar_dates.txt", "service_id,date,exception_type\n" + dates)
        if trips:
            zf.writestr("trips.txt", "route_id,service_id,trip_id\n" + trips)
        if feed_info:
            zf.writestr("feed_info.txt", "feed_publisher_name,feed_publisher_url,feed_lang,feed_start_date,feed_end_date\n" + feed_info)
    return read_feed(path, CONFIG).tables


class ServiceCalendarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_flags_exceptions_and_holidays(self):
        tables = feed(self.dir, "a.zip",
                      "WK,1,1,1,1,1,0,0,20260105,20260131\nHD,0,0,0,0,0,0,1,20260105,20260131\n",
                      dates="HD,20260112,1\nWK,20260112,2\nWK,20260120,2\n")
        cal = build_calendar(tables, HOL, CONFIG)
        # Without feed_info or catalogue validity the window spans the first and last service day.
        self.assertEqual(cal.window, (date(2026, 1, 5), date(2026, 1, 30)))
        self.assertEqual(cal.days[date(2026, 1, 6)], frozenset({"WK"}))
        self.assertEqual(cal.days[date(2026, 1, 12)], frozenset({"HD"}))  # Coming of Age Day
        # The holiday runs the Sunday service, so holidays and Sundays form one day type.
        self.assertEqual(cal.day_types[date(2026, 1, 12)], "sun,hol")
        self.assertEqual(sorted(set(cal.groups.values())), [WEEKDAYS, "sat", "sun,hol"])
        self.assertEqual(cal.days[date(2026, 1, 20)], frozenset())  # removed
        self.assertEqual(cal.active_days(WEEKDAYS.split(",")), 18)

    def test_unused_services_are_ignored(self):
        tables = feed(self.dir, "b.zip", "WK,1,1,1,1,1,0,0,20260105,20260131\nX,1,1,1,1,1,1,1,20260105,20260131\n",
                      trips="R1,WK,T1\n")
        cal = build_calendar(tables, HOL, CONFIG)
        self.assertEqual(cal.days[date(2026, 1, 6)], frozenset({"WK"}))

    def test_special_days_and_periods(self):
        # Regular weekday service, a short special service at the start of January, and a timetable change on 2026-03-01.
        tables = feed(self.dir, "c.zip",
                      "OLD,1,1,1,1,1,0,0,20260105,20260228\nNEW,1,1,1,1,1,0,0,20260301,20260331\n"
                      "NY,0,0,0,0,0,0,0,20260105,20260105\n",
                      dates="NY,20260105,1\nNY,20260106,1\nOLD,20260105,2\nOLD,20260106,2\n")
        cal = build_calendar(tables, HOL, CONFIG)
        # Holidays run the weekday timetable here, so they join the weekday day type; only the two
        # New Year days are special.
        self.assertEqual(cal.groups["hol"], WEEKDAYS + ",hol")
        self.assertEqual({d.isoformat() for d in cal.special}, {"2026-01-05", "2026-01-06"})
        weekday = [(p.start.isoformat(), p.end.isoformat(), sorted(p.services)) for p in cal.periods if p.day_type == WEEKDAYS + ",hol"]
        self.assertEqual(weekday, [("2026-01-07", "2026-02-27", ["OLD"]), ("2026-03-02", "2026-03-31", ["NEW"])])

    def test_window_from_feed_info_and_clipping(self):
        tables = feed(self.dir, "d.zip", "WK,1,1,1,1,1,0,0,20250101,20991231\n",
                      feed_info="p,https://example.jp,ja,20260101,20991231\n")
        cal = build_calendar(tables, HOL, CONFIG)
        self.assertEqual(cal.window, (date(2026, 1, 1), date(HOL.last_year, 12, 31)))
        self.assertIn("WINDOW_CLIPPED_TO_HOLIDAY_TABLE", cal.notes)

    def test_no_service_days(self):
        cal = build_calendar(feed(self.dir, "e.zip", ""), HOL, CONFIG)
        self.assertIsNone(cal.window)
        self.assertEqual(cal.notes, ["NO_SERVICE_DAYS"])


class DayGroupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tuesday_to_friday_timetable(self):
        tables = feed(self.dir, "t.zip", "MO,1,0,0,0,0,0,0,20260601,20260731\nTF,0,1,1,1,1,0,0,20260601,20260731\n"
                                         "WE,0,0,0,0,0,1,1,20260601,20260731\n")
        cal = build_calendar(tables, HOL, CONFIG)
        # 2026-07-20 (Marine Day) is a Monday running the Monday service, so it joins Monday.
        self.assertEqual(sorted(set(cal.groups.values())), ["mon,hol", "sat,sun", "tue,wed,thu,fri"])

    def test_same_trips_under_other_service_ids(self):
        # Every weekday has its own service id but the same trip: one day type.
        cal = "".join(f"{d},{','.join('1' if i == k else '0' for i in range(7))},20260601,20260731\n" for k, d in enumerate(["M", "T", "W", "R", "F"]))
        trips = "".join(f"R1,{d},T{d}\n" for d in "MTWRF")
        tables = feed(self.dir, "s.zip", cal, trips=trips)
        path = self.dir / "s.zip"
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n" +
                        "".join(f"T{d},08:00:00,08:00:00,A,1\nT{d},08:10:00,08:10:00,B,2\n" for d in "MTWRF"))
        cal_ = build_calendar(read_feed(path, CONFIG).tables, HOL, CONFIG)
        self.assertEqual(cal_.groups["mon"], "mon,tue,wed,thu,fri,hol")

    def test_common_refinement(self):
        old = build_calendar(feed(self.dir, "o.zip", "WK,1,1,1,1,1,0,0,20260601,20260731\n"), HOL, CONFIG)
        new = build_calendar(feed(self.dir, "n.zip", "MO,1,0,0,0,0,0,0,20260601,20260731\nTF,0,1,1,1,1,0,0,20260601,20260731\n"),
                             HOL, CONFIG)
        self.assertEqual(sorted(set(old.groups.values())), ["mon,tue,wed,thu,fri,hol", "sat,sun"])
        self.assertEqual(list(choose_comparison(old, new, CONFIG).day_types), ["mon,hol", "tue,wed,thu,fri", "sat,sun"])


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_successive_periods_without_overlap(self):
        old = build_calendar(feed(self.dir, "o.zip", "A,1,1,1,1,1,1,0,20250401,20260331\n"), HOL, CONFIG)
        new = build_calendar(feed(self.dir, "n.zip", "B,1,1,1,1,1,0,1,20260401,20270331\n"), HOL, CONFIG)
        cmp = choose_comparison(old, new, CONFIG)
        self.assertEqual(cmp.mode, "successive_periods")
        # Old: Mon-Sat (+hol) / Sun; new: every day but Saturday. Common refinement: weekdays, Sat, Sun.
        self.assertEqual(list(cmp.day_types), [WEEKDAYS + ",hol", "sat", "sun"])
        wk = cmp.day_types[WEEKDAYS + ",hol"]
        self.assertEqual((wk.old_date, wk.new_date), (date(2026, 3, 31), date(2026, 4, 1)))
        self.assertEqual((wk.old_services, wk.new_services), (frozenset({"A"}), frozenset({"B"})))
        sat = cmp.day_types["sat"]
        self.assertEqual((sat.old_services, sat.new_services), (frozenset({"A"}), frozenset()))

    def test_successive_periods_use_the_dominant_timetable(self):
        # School-term trips (S) run on most weekdays; the last and first weeks are school holidays.
        old = build_calendar(feed(self.dir, "o.zip", "W,1,1,1,1,1,0,0,20250401,20260331\nS,1,1,1,1,1,0,0,20250407,20260320\n"), HOL, CONFIG)
        new = build_calendar(feed(self.dir, "n.zip", "W,1,1,1,1,1,0,0,20260401,20270331\nS,1,1,1,1,1,0,0,20260407,20270319\n"), HOL, CONFIG)
        wk = choose_comparison(old, new, CONFIG).day_types[WEEKDAYS + ",hol"]
        self.assertEqual((wk.old_services, wk.new_services), (frozenset({"W", "S"}), frozenset({"W", "S"})))
        self.assertEqual((wk.old_date, wk.new_date), (date(2026, 3, 20), date(2026, 4, 7)))  # 3/20: a holiday on the weekday timetable

    def test_same_days_with_overlap(self):
        # The old publication already contained the timetable from 2026-04-01 (a bundled publication).
        old = build_calendar(feed(self.dir, "o.zip", "A,1,1,1,1,1,0,0,20250401,20260331\nB,1,1,1,1,1,0,0,20260401,20260930\n"), HOL, CONFIG)
        new = build_calendar(feed(self.dir, "n.zip", "B,1,1,1,1,1,0,0,20260401,20270331\n"), HOL, CONFIG)
        cmp = choose_comparison(old, new, CONFIG)
        self.assertEqual(cmp.mode, "same_days")
        wk = cmp.day_types[WEEKDAYS + ",hol"]
        self.assertEqual((wk.old_services, wk.new_services), (frozenset({"B"}), frozenset({"B"})))
        self.assertEqual(wk.old_date, wk.new_date)
        self.assertEqual(len([p for p in old.periods if p.day_type == WEEKDAYS + ",hol"]), 2)  # both periods are known


if __name__ == "__main__":
    unittest.main()
