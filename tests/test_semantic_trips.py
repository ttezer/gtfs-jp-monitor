import unittest

from gtfs_jp_semantic.reader import Config, Table
from gtfs_jp_semantic.trips import (
    Trip,
    build_trips,
    direction_key,
    dominant_pattern,
    match_trips,
    parse_minutes,
    pattern_edits,
)

CFG = Config.load().matching


def trip(tid, places, start, step=5, line="1", direction="0", times=None):
    return Trip(tid, line, direction, tuple(places), tuple(times) if times else tuple(start + step * i for i in range(len(places))))


def kinds(pairs):
    return sorted((p.old, p.new, p.kind) for p in pairs if p.old is not None) + sorted((p.old, p.new, p.kind) for p in pairs if p.old is None)


class ParseTest(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(parse_minutes("06:40:00"), 400)
        self.assertEqual(parse_minutes("25:10:30"), 1510)  # after midnight
        self.assertEqual(parse_minutes(" 6:05:00"), 365)
        for bad in ("", "6:05", "06:61:00", "aa:bb:cc"):
            self.assertIsNone(parse_minutes(bad), bad)

    def test_direction_key(self):
        self.assertEqual(direction_key("1", ("A", "B")), "1")
        self.assertEqual(len(direction_key("", ("A", "B"))), 8)
        self.assertNotEqual(direction_key("", ("A", "B")), direction_key("", ("B", "A")))


class BuildTripsTest(unittest.TestCase):
    def test_day_services_translation_and_order(self):
        def tbl(name, header, rows):
            return Table(name=name, sha256="", status="ok", header=tuple(header), rows=[tuple(r) for r in rows], row_count=len(rows))
        tables = {
            "trips.txt": tbl("trips.txt", ["route_id", "service_id", "trip_id", "direction_id"],
                             [("R1", "WK", "T2", "0"), ("R1", "WK", "T1", "0"), ("R1", "SA", "T3", "0"), ("RX", "WK", "T4", "0")]),
            "stop_times.txt": tbl("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"], [
                ("T1", "07:00:00", "07:00:00", "s1", "1"), ("T1", "07:10:00", "", "s2", "10"),
                ("T2", "06:30:00", "06:31:00", "s1", "1"), ("T2", "06:40:00", "06:40:00", "s9", "2"),
                ("T3", "08:00:00", "08:00:00", "s1", "1"),
            ]),
        }
        trips = build_trips(tables, frozenset({"WK"}), {"R1": "1"}, {"s1": "P1", "s2": "P2", "s9": "P9"},
                            translate={"P1": "N1", "P2": "N2"})
        self.assertEqual([t.trip_id for t in trips], ["T2", "T1"])  # by first departure; T3 other day, T4 unknown route
        self.assertEqual(trips[0].places, ("N1", "old:P9"))  # unmatched old place keeps a distinct id
        self.assertEqual(trips[0].times, (391, 400))
        self.assertEqual(trips[1].times, (420, 430))  # arrival used when departure is empty


class MatchTripsTest(unittest.TestCase):
    def test_exact_with_new_ids(self):
        pairs = match_trips([trip("a", "ABC", 400)], [trip("x", "ABC", 400)], CFG)
        self.assertEqual(kinds(pairs), [(0, 0, "exact")])

    def test_retimed_rerouted_added_removed(self):
        old = [trip("a", "ABCD", 400), trip("b", "ABCD", 460), trip("c", "ABCD", 900)]
        new = [trip("x", "ABCD", 405), trip("y", "ABCE", 460), trip("z", "ABCD", 1200)]
        self.assertEqual(kinds(match_trips(old, new, CFG)),
                         [(0, 0, "retimed"), (1, 1, "rerouted"), (2, None, "removed"), (None, 2, "added")])

    def test_rerouted_and_retimed(self):
        pairs = match_trips([trip("a", "ABCD", 400)], [trip("x", "ABCE", 410)], CFG)
        self.assertEqual(kinds(pairs), [(0, 0, "retimed_rerouted")])

    def test_nearest_in_time_wins(self):
        old = [trip("a", "ABC", 400), trip("b", "ABC", 430)]
        new = [trip("x", "ABC", 425), trip("y", "ABC", 402)]
        self.assertEqual(kinds(match_trips(old, new, CFG)), [(0, 1, "retimed"), (1, 0, "retimed")])

    def test_limits(self):
        far = match_trips([trip("a", "ABC", 400)], [trip("x", "ABC", 400 + CFG["trip_max_shift_min"] + 1)], CFG)
        self.assertEqual(kinds(far), [(0, None, "removed"), (None, 0, "added")])
        other_route = match_trips([trip("a", "ABCDEFGH", 400)], [trip("x", "AXYZ", 400)], CFG)
        self.assertEqual(kinds(other_route), [(0, None, "removed"), (None, 0, "added")])

    def test_every_trip_appears_once(self):
        old = [trip(f"o{i}", "ABC", 300 + 20 * i) for i in range(10)]
        new = [trip(f"n{i}", "ABC", 305 + 30 * i) for i in range(7)]
        pairs = match_trips(old, new, CFG)
        self.assertEqual(sorted(p.old for p in pairs if p.old is not None), list(range(10)))
        self.assertEqual(sorted(p.new for p in pairs if p.new is not None), list(range(7)))


class PatternTest(unittest.TestCase):
    def test_dominant(self):
        trips = [trip("a", "ABC", 1), trip("b", "ABC", 2), trip("c", "ABCD", 3)]
        self.assertEqual(dominant_pattern(trips), tuple("ABC"))

    def test_edits(self):
        cases = {
            ("ABC", "ABCD"): [("extended", ("D",))],
            ("ABC", "XABC"): [("extended", ("X",))],
            ("ABCD", "ABC"): [("shortened", ("D",))],
            ("ABCD", "BCD"): [("shortened", ("A",))],
            ("ABCD", "ABXCD"): [("inserted", ("X",))],
            ("ABXCD", "ABCD"): [("removed", ("X",))],
            ("ABCD", "ABXYCD"): [("detour_added", ("X", "Y"))],
            ("ABXYCD", "ABCD"): [("detour_removed", ("X", "Y"))],
            ("ABCD", "ABCD"): [],
        }
        for (a, b), expected in cases.items():
            with self.subTest(old=a, new=b):
                self.assertEqual([(e.kind, e.places) for e in pattern_edits(tuple(a), tuple(b))], expected)


if __name__ == "__main__":
    unittest.main()
