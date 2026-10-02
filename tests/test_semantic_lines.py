import re
import unicodedata
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

# Line family settings to be enabled with the next engine version (WORKPLAN §18 open item 7); until
# then the default configuration leaves them empty so reports of the current version do not change.
FAMILY_CFG = dict(CFG, line_family_patterns=[r"^\[([^\]]+)\]"],
                  line_name_strip_patterns=[r"^[A-Z][0-9]*(?=[^A-Za-z0-9])", r"線$"])


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

    def test_family_pattern(self):
        families = [re.compile(p) for p in FAMILY_CFG["line_family_patterns"]]
        route = {"route_short_name": "", "route_long_name": "［市振線］早朝便（市振～泊駅）", "route_id": "A2"}
        self.assertEqual(line_key(route, families), "市振線")
        self.assertEqual(line_key(route), "[市振線]早朝便(市振~泊駅)")  # no pattern configured
        self.assertEqual(line_key({"route_short_name": "［］", "route_id": "x"}, families), "[]")  # empty family is not used

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

    def test_equal_names_after_removing_line_codes(self):
        old = feed({"R1": ("宮崎境線", ""), "R2": ("A線", "")}, {"T1": ("R1", list("ABC")), "T2": ("R2", list("DE"))})
        new = feed({"N1": ("A1宮崎境線", ""), "N2": ("B線", "")}, {"U1": ("N1", list("XYZ")), "U2": ("N2", list("VW"))})
        m = run(old, new)
        self.assertEqual(m[(("宮崎境線",), ())].relation, "discontinued")  # nothing configured: names differ
        m = {(x.old, x.new): x for x in match_lines(build_lines(old, IDENTITY, FAMILY_CFG), build_lines(new, IDENTITY, FAMILY_CFG),
                                                    IDENTITY, FAMILY_CFG)}
        match = m[(("宮崎境線",), ("A1宮崎境線",))]
        self.assertEqual((match.relation, match.method, match.confidence), ("renamed", "same_line_name", 1.0))
        # A name that would become empty keeps its full form, so "A線" and "B線" stay apart.
        self.assertEqual((m[(("A線",), ())].relation, m[((), ("B線",))].relation), ("discontinued", "added"))

    def test_lines_sharing_a_name_take_its_shape(self):
        old = feed({"R1": ("A1宮崎境線", ""), "R2": ("A2宮崎境線", "")}, {"T1": ("R1", list("ABC")), "T2": ("R2", list("DEF"))})
        new = feed({"N1": ("宮崎境線", "")}, {"U1": ("N1", list("XYZ"))})
        m = match_lines(build_lines(old, IDENTITY, FAMILY_CFG), build_lines(new, IDENTITY, FAMILY_CFG), IDENTITY, FAMILY_CFG)
        self.assertEqual([(x.old, x.new, x.relation) for x in m], [(("A1宮崎境線", "A2宮崎境線"), ("宮崎境線",), "merged")])

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

    def test_route_variants_regrouped_under_line_codes(self):
        # Modelled on toyama-asahitown/asahimachibus 9989e43c -> 51d03588. The old publication has one
        # route per trip variant, named only in route_long_name as "［family］variant（…）", with the
        # line code in route_id; the new one has one route per line, named by code + family. Every line
        # starts at 泊駅 (T), and lines share an eastern (E*) or western (W*) trunk, which chains them
        # into components larger than line_max_component. Ground truth (decided by the user): the
        # network is the same, so each family is one line renamed; nothing is discontinued or added.
        # 愛本線's one-way trips become round trips (a pattern change, still renamed).
        east, west = ["T", "E1", "E2", "E3", "E4"], ["T", "W1", "W2", "W3"]
        old_variants = {  # route_id: (route_long_name, stops of its trip)
            "A1 宮崎境線(miyazakisakai01)": ("［宮崎境線］（泊駅～宮崎境方面～泊駅）", east + ["M1", "M2", "T"]),
            "A1 宮崎境線(miyazakisakai02)": ("［宮崎境線］夜便（泊駅～宮崎境方面～泊駅）", east + ["M1", "T"]),
            "A1 宮崎境線(miyazakisakai03)": ("［宮崎境線］役場便（泊駅～役場～宮崎境方面～役場～泊駅）", ["T", "Y"] + east[1:] + ["M1", "M2", "Y", "T"]),
            "A2 市振線(ichiburi002)": ("［市振線］（泊駅～市振～泊駅）", east + ["I1", "I2", "O1", "T"]),
            "A2 市振線(ichiburi004)": ("［市振線］※大平なし（泊駅～市振～泊駅）", east + ["I1", "I2", "T"]),
            "A2 市振線(ichiburi001)": ("［市振線］早朝便（市振～泊駅）", ["I2", "I1", "E4", "E3", "E2", "E1", "T"]),
            "B 笹川線(sasagawa02)": ("［笹川線］（泊駅～笹川方面～泊駅）", ["T", "E1", "E2", "S1", "S2", "S3", "T"]),
            "B 笹川線(sasagawa01)": ("［笹川線］朝便（泊駅～笹川方面～泊駅）", ["T", "E1", "E2", "S1", "S2", "T"]),
            "C 草野赤川線(kusanoakagawa02)": ("［草野赤川線］（泊駅～役場～草野赤川方面～泊駅）", ["T", "Y", "K1", "K2", "K3", "T"]),
            "D1 南保線(nanbo02)": ("［南保線］始発便（泊駅～南保方面～泊駅）", west + ["N1", "N2", "T"]),
            "D1 南保線(nanbo04)": ("［南保線］最終便（泊駅～南保方面～泊駅）", west + ["N1", "T"]),
            "D1 南保線(nanbo05)": ("［南保線］（泊駅～南保方面～泊駅）", west + ["N1", "N2", "T"]),
            "D2 山崎線(yamazaki01)": ("［山崎線］朝便（泊駅～山崎方面～泊駅）", west + ["Z1", "T"]),
            "D2 山崎線(yamazaki02)": ("［山崎線］（泊駅～山崎方面～泊駅）", west + ["Z1", "Z2", "T"]),
            "E1 藤塚線(fujizuka0001)": ("［藤塚線］（泊駅～藤塚～泊駅）", ["T", "F1", "F2", "F3", "T"]),
            "E2 愛本線(aimoto0003)": ("［愛本線］（泊駅～愛本方面）", west + ["H1", "H2"]),
            "E2 愛本線(aimoto0004)": ("［愛本線］（愛本方面～泊駅）", ["H2", "H1", "W3", "W2", "W1", "T"]),
            "E2 愛本線(aimoto0007)": ("［愛本線］（愛本方面～GS経由～泊駅）", ["H2", "H1", "G", "W2", "W1", "T"]),
            "Ｆ大家庄線(ooiesyouam-2)": ("［大家庄線］（泊駅～大家庄方面～泊駅）", ["T", "W1", "L1", "L2", "L3", "T"]),
            "Ｆ大家庄線(ooiesyouam-1)": ("［大家庄線］朝便（大家庄方面～泊駅）", ["L3", "L2", "L1", "W1", "T"]),
        }
        new_lines = {  # route_id: (route_short_name, stops of its trips)
            "1": ("A1宮崎境線", [east + ["M1", "M2", "T"], ["T", "Y"] + east[1:] + ["M1", "M2", "Y", "T"]]),
            "2": ("A2市振線", [east + ["I1", "I2", "T"], ["I2", "I1", "E4", "E3", "E2", "E1", "T"]]),
            "3": ("B笹川線", [["T", "E1", "E2", "S1", "S2", "S3", "T"]]),
            "4": ("C草野赤川線", [["T", "Y", "K1", "K2", "K3", "T"]]),
            "5": ("D1南保線", [west + ["N1", "N2", "T"]]),
            "6": ("D2山崎線", [west + ["Z1", "Z2", "T"]]),
            "7": ("E1藤塚線", [["T", "F1", "F2", "F3", "T"]]),
            "8": ("E2愛本線", [west + ["H1", "H2", "H1", "W3", "W2", "W1", "T"]]),
            "9": ("F大家庄", [["T", "W1", "L1", "L2", "L3", "T"]]),
        }
        old = feed({rid: ("", name) for rid, (name, _) in old_variants.items()},
                   {f"t{i}": (rid, stops) for i, (rid, (_, stops)) in enumerate(old_variants.items())})
        new = feed({rid: (name, "") for rid, (name, _) in new_lines.items()},
                   {f"u{rid}{j}": (rid, stops) for rid, (_, trips) in new_lines.items() for j, stops in enumerate(trips)})
        places = {s: s for _, stops in old_variants.values() for s in stops}
        old_lines, new_lines_ = build_lines(old, places, FAMILY_CFG), build_lines(new, places, FAMILY_CFG)
        result = match_lines(old_lines, new_lines_, places, FAMILY_CFG)

        self.assertEqual(sorted(m.relation for m in result), ["renamed"] * 9)
        # Every old route lands in the new line its route_id code names ("A2 市振線(…)" -> "A2市振線").
        code = lambda text: re.match(r"[A-Z][0-9]?", unicodedata.normalize("NFKC", text)).group()
        landed = {r: m.new for m in result for k in m.old for r in old_lines[k].route_ids}
        self.assertEqual(set(landed), set(old_variants))
        for route_id, (new_key,) in landed.items():
            self.assertTrue(new_key.startswith(code(route_id)), (route_id, new_key))

    def test_deterministic(self):
        old = feed({"R1": ("北線", ""), "R2": ("南線", "")}, {"T1": ("R1", list("ABC")), "T2": ("R2", list("DEF"))})
        new = feed({"R3": ("環状線", "")}, {"T3": ("R3", list("ABCDEF"))})
        a = match_lines(build_lines(old, IDENTITY), build_lines(new, IDENTITY), IDENTITY, CFG)
        b = match_lines(build_lines(old, IDENTITY), build_lines(new, IDENTITY), IDENTITY, CFG)
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
