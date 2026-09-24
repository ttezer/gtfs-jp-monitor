import unittest

from gtfs_jp_semantic.places import NameNormaliser, build_places, match_places, place_of_stop
from gtfs_jp_semantic.reader import Config, Table

CFG = Config.load().matching
HEADER = ("stop_id", "stop_name", "stop_lat", "stop_lon", "location_type", "parent_station")


def table(rows):
    return Table(name="stops.txt", sha256="", status="ok", header=HEADER, rows=[tuple(r) for r in rows], row_count=len(rows))


def by_old(matches):
    return {(m.old.place_id if m.old else None, m.new.place_id if m.new else None): m for m in matches}


# ~0.0009 degrees of latitude is about 100 m.
BASE_LAT, BASE_LON = 33.0, 131.0


class NormaliserTest(unittest.TestCase):
    def setUp(self):
        self.norm = NameNormaliser(CFG["stop_name_suffixes"], CFG["stop_name_strip_patterns"])

    def test_platform_and_direction_suffixes(self):
        self.assertEqual(self.norm("中津駅（上り）"), "中津駅")
        self.assertEqual(self.norm("中津駅 (下り)"), "中津駅")
        self.assertEqual(self.norm("中津駅 2番のりば"), "中津駅")
        self.assertEqual(self.norm("中津駅A"), "中津駅")

    def test_real_names_are_not_broken(self):
        self.assertEqual(self.norm("津名一宮IC"), "津名一宮IC")
        self.assertEqual(self.norm("アイセル21"), "アイセル21")
        self.assertEqual(self.norm("のりば"), "のりば")  # never strips a name down to nothing

    def test_full_width_forms(self):
        self.assertEqual(self.norm("ＪＲ中津駅"), self.norm("JR中津駅"))


class BuildPlacesTest(unittest.TestCase):
    def test_parents_children_and_other_location_types(self):
        places = build_places(table([
            ("ST", "中津駅", "33.6", "131.2", "1", ""),
            ("ST-1", "中津駅 1番のりば", "33.6001", "131.2", "0", "ST"),
            ("ST-2", "中津駅 2番のりば", "33.6002", "131.2", "0", "ST"),
            ("ST-E", "中津駅 北口", "33.6003", "131.2", "2", "ST"),
            ("S1", "市役所", "33.61", "131.21", "", ""),
            ("ORPHAN", "孤立", "33.62", "131.22", "0", "MISSING"),
        ]))
        self.assertEqual(sorted(places), ["ORPHAN", "S1", "ST"])
        self.assertEqual(places["ST"].members, ("ST", "ST-1", "ST-2"))
        self.assertEqual(place_of_stop(places)["ST-2"], "ST")
        self.assertNotIn("ST-E", place_of_stop(places))

    def test_parent_without_coordinates_uses_children(self):
        places = build_places(table([
            ("ST", "駅", "", "", "1", ""),
            ("A", "駅A", "33.0", "131.0", "0", "ST"),
            ("B", "駅B", "33.002", "131.0", "0", "ST"),
        ]))
        self.assertAlmostEqual(places["ST"].lat, 33.001)


class MatchPlacesTest(unittest.TestCase):
    def match(self, old_rows, new_rows):
        return by_old(match_places(build_places(table(old_rows)), build_places(table(new_rows)), CFG))

    def test_same_id_unchanged_renamed_moved(self):
        m = self.match(
            [("S1", "駅前", "33.0", "131.0", "", ""), ("S2", "市役所", "33.01", "131.0", "", ""),
             ("S3", "病院", "33.02", "131.0", "", "")],
            [("S1", "駅前", "33.0", "131.0", "", ""), ("S2", "市役所前", "33.01", "131.0", "", ""),
             ("S3", "病院", "33.0203", "131.0", "", "")],
        )
        self.assertEqual(m[("S1", "S1")].status, "unchanged")
        self.assertEqual((m[("S2", "S2")].status, m[("S2", "S2")].confidence), ("renamed", 0.9))
        self.assertEqual(m[("S3", "S3")].status, "moved")
        self.assertEqual(m[("S3", "S3")].distance_m, 33)

    def test_reused_id_is_not_trusted(self):
        # S9 now names a stop 5 km away with another name: not the same place.
        m = self.match([("S9", "旧駅", "33.0", "131.0", "", "")], [("S9", "新市街", "33.045", "131.0", "", "")])
        self.assertIn(("S9", None), m)
        self.assertIn((None, "S9"), m)

    def test_renumbered_ids(self):
        m = self.match(
            [("100", "中津駅（上り）", "33.6", "131.2", "", ""), ("101", "市役所", "33.61", "131.2", "", "")],
            [("A-1", "中津駅", "33.6002", "131.2", "", ""), ("A-2", "市役所", "33.6101", "131.2", "", "")],
        )
        self.assertEqual(m[("100", "A-1")].method, "same_name")
        self.assertEqual(m[("101", "A-2")].method, "same_name")

    def test_close_similar_name(self):
        m = self.match([("X", "中央公民館", "33.0", "131.0", "", "")], [("Y", "中央公民館前", "33.0005", "131.0", "", "")])
        self.assertEqual(m[("X", "Y")].method, "near_similar_name")
        self.assertEqual(m[("X", "Y")].status, "renamed_moved")

    def test_one_to_one_nearest_wins(self):
        m = self.match(
            [("O", "団地", "33.0", "131.0", "", "")],
            [("N1", "団地", "33.003", "131.0", "", ""), ("N2", "団地", "33.0005", "131.0", "", "")],
        )
        self.assertIn(("O", "N2"), m)
        self.assertIn((None, "N1"), m)

    def test_missing_coordinates_only_match_by_id_and_name(self):
        m = self.match([("S1", "駅前", "", "", "", ""), ("S2", "病院", "", "", "", "")],
                       [("S1", "駅前", "", "", "", ""), ("T2", "病院", "", "", "", "")])
        self.assertEqual(m[("S1", "S1")].status, "unchanged")
        self.assertIn(("S2", None), m)

    def test_added_and_removed(self):
        m = self.match([("S1", "駅前", "33.0", "131.0", "", "")], [("S5", "新団地", "33.05", "131.0", "", "")])
        self.assertEqual(m[("S1", None)].status, "removed")
        self.assertEqual(m[(None, "S5")].status, "added")

    def test_deterministic(self):
        old = [("A", "団地", "33.0", "131.0", "", ""), ("B", "団地", "33.0001", "131.0", "", "")]
        new = [("C", "団地", "33.00005", "131.0", "", ""), ("D", "団地", "33.00015", "131.0", "", "")]
        first = [(x.old and x.old.place_id, x.new and x.new.place_id) for x in match_places(build_places(table(old)), build_places(table(new)), CFG)]
        again = [(x.old and x.old.place_id, x.new and x.new.place_id) for x in match_places(build_places(table(list(reversed(old)))), build_places(table(list(reversed(new)))), CFG)]
        self.assertEqual(first, again)


if __name__ == "__main__":
    unittest.main()
