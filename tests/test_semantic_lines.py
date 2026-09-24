import unittest

from gtfs_jp_semantic.lines import build_lines, line_key, match_lines
from gtfs_jp_semantic.reader import Config, Table

CFG = Config.load().matching


def tbl(name, header, rows):
    return Table(name=name, sha256="", status="ok", header=tuple(header), rows=[tuple(r) for r in rows], row_count=len(rows))


def feed(routes, trips):
    """routes: {route_id: (short, long)}; trips: {trip_id: (route_id, [stop ids])}."""
    return {
        "routes.txt": tbl("routes.txt", ["route_id", "route_short_name", "route_long_name"],
                          [(rid, s, l) for rid, (s, l) in routes.items()]),
        "trips.txt": tbl("trips.txt", ["route_id", "service_id", "trip_id"], [(r, "WK", t) for t, (r, _) in trips.items()]),
        "stop_times.txt": tbl("stop_times.txt", ["trip_id", "stop_sequence", "stop_id"],
                              [(t, str(i), s) for t, (_, stops) in trips.items() for i, s in enumerate(stops, 1)]),
    }


IDENTITY = {s: s for s in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}


def run(old_feed, new_feed, place_map=None):
    old = build_lines(old_feed, IDENTITY)
    new = build_lines(new_feed, IDENTITY)
    return {(m.old, m.new): m for m in match_lines(old, new, place_map if place_map is not None else IDENTITY, CFG)}


class LineKeyTest(unittest.TestCase):
    def test_short_then_long_then_id(self):
        self.assertEqual(line_key({"route_short_name": " 1 ", "route_long_name": "駅前線", "route_id": "R1"}), "1")
        self.assertEqual(line_key({"route_short_name": "", "route_long_name": "駅前 線", "route_id": "R1"}), "駅前線")
        self.assertEqual(line_key({"route_short_name": "", "route_long_name": "", "route_id": "R1"}), "R1")
        self.assertEqual(line_key({"route_short_name": "１", "route_id": "x"}), "1")  # NFKC

    def test_routes_with_one_name_form_one_line(self):
        lines = build_lines(feed({"R1": ("1", ""), "R1b": ("1", "")}, {"T1": ("R1", ["A", "B"]), "T2": ("R1b", ["B", "C"])}), IDENTITY)
        self.assertEqual(list(lines), ["1"])
        self.assertEqual(lines["1"].route_ids, ("R1", "R1b"))
        self.assertEqual(lines["1"].places, frozenset("ABC"))


class MatchLinesTest(unittest.TestCase):
    def test_same_name(self):
        m = run(feed({"R1": ("1", "")}, {"T": ("R1", ["A", "B"])}), feed({"X": ("1", "")}, {"T": ("X", ["A", "C"])}))
        self.assertEqual(m[(("1",), ("1",))].relation, "same")

    def test_renamed_by_served_places(self):
        m = run(feed({"R1": ("駅前線", "")}, {"T": ("R1", list("ABCDE"))}),
                feed({"R9": ("市役所線", "")}, {"T": ("R9", list("ABCDF"))}))
        match = m[(("駅前線",), ("市役所線",))]
        self.assertEqual((match.relation, match.method, match.confidence), ("renamed", "served_places", 0.8))

    def test_merge_and_split(self):
        old = feed({"R1": ("北線", ""), "R2": ("南線", "")}, {"T1": ("R1", list("ABC")), "T2": ("R2", list("DEF"))})
        new = feed({"R3": ("環状線", "")}, {"T3": ("R3", list("ABCDEF"))})
        merged = run(old, new)[(("北線", "南線"), ("環状線",))]
        self.assertEqual(merged.relation, "merged")
        split = run(new, old)[(("環状線",), ("北線", "南線"))]
        self.assertEqual(split.relation, "split")

    def test_discontinued_and_added(self):
        m = run(feed({"R1": ("1", "")}, {"T": ("R1", list("ABC"))}), feed({"R2": ("2", "")}, {"T": ("R2", list("XYZ"))}))
        self.assertEqual(m[(("1",), ())].relation, "discontinued")
        self.assertEqual(m[((), ("2",))].relation, "added")

    def test_place_matches_translate_renumbered_stops(self):
        old = feed({"R1": ("旧線", "")}, {"T": ("R1", ["a", "b", "c"])})
        new = feed({"R2": ("新線", "")}, {"T": ("R2", ["A", "B", "C"])})
        old_lines = build_lines(old, {"a": "a", "b": "b", "c": "c"})
        new_lines = build_lines(new, IDENTITY)
        result = match_lines(old_lines, new_lines, {"a": "A", "b": "B", "c": "C"}, CFG)
        self.assertEqual([(r.old, r.new, r.relation) for r in result], [(("旧線",), ("新線",), "renamed")])

    def test_large_components_are_not_interpreted(self):
        # Five old lines all overlapping one corridor served by two new lines: 7 > line_max_component.
        old = feed({f"O{i}": (f"旧{i}", "") for i in range(5)}, {f"T{i}": (f"O{i}", list("ABCD")) for i in range(5)})
        new = feed({"N1": ("新1", ""), "N2": ("新2", "")}, {"U1": ("N1", list("ABCD")), "U2": ("N2", list("ABCD"))})
        relations = sorted(m.relation for m in run(old, new).values())
        self.assertEqual(relations, ["added"] * 2 + ["discontinued"] * 5)

    def test_deterministic(self):
        old = feed({"R1": ("北線", ""), "R2": ("南線", "")}, {"T1": ("R1", list("ABC")), "T2": ("R2", list("DEF"))})
        new = feed({"R3": ("環状線", "")}, {"T3": ("R3", list("ABCDEF"))})
        a = match_lines(build_lines(old, IDENTITY), build_lines(new, IDENTITY), IDENTITY, CFG)
        b = match_lines(build_lines(old, IDENTITY), build_lines(new, IDENTITY), IDENTITY, CFG)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
