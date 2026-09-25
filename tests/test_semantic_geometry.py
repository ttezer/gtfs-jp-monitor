import unittest

from gtfs_jp_semantic.geometry import compare, encode, length_m, simplify

# About 111 m per 0.001 degree of latitude.
def north(n, lon=135.0, step=0.001):
    return [(35.0 + i * step, lon) for i in range(n)]


class SimplifyTest(unittest.TestCase):
    def test_straight_line_keeps_ends(self):
        self.assertEqual(simplify(north(20), 10), [north(20)[0], north(20)[-1]])

    def test_corner_is_kept(self):
        line = north(5) + [(35.004, 135.0 + i * 0.001) for i in range(1, 5)]
        self.assertEqual(len(simplify(line, 10)), 3)

    def test_encode_and_length(self):
        self.assertEqual(encode([(35.123456, 135.654321)]), [[3512346, 13565432]])
        self.assertAlmostEqual(length_m(north(11)), 1112, delta=5)


class CompareTest(unittest.TestCase):
    def test_same_line(self):
        r = compare(north(11), north(11), 20, 50, 500)
        self.assertEqual((r["max_m"], r["diverged_new_m"], r["diverged_old_m"], r["new_spans"]), (0, 0, 0, []))

    def test_detour(self):
        old = north(11)
        # From 35.004 to 35.006 the new line runs 300 m east (0.0033 degrees of longitude at 35N).
        new = north(5) + [(35.004, 135.0033), (35.006, 135.0033)] + north(11)[6:]
        r = compare(old, new, 20, 50, 500)
        self.assertGreater(r["max_m"], 250)
        self.assertLess(r["max_m"], 320)
        self.assertGreater(r["diverged_new_m"], 400)  # the eastern leg and the two connectors beyond 50 m
        self.assertEqual(len(r["new_spans"]), 1)
        first, last = r["new_spans"][0]
        self.assertTrue(4 <= first and last <= 7)

    def test_stop_with_swapped_coordinates(self):
        # A stop at (lat 134.98, lon 34.58) makes a segment of thousands of kilometres; indexing its
        # bounding box took more than 8 GiB. A point next to that segment must still be found.
        old = north(11)
        new = north(11) + [(134.98, 34.58)]
        r = compare(old, new, 20, 50, 500)
        self.assertEqual((r["max_m"], r["capped"]), (500, True))
        self.assertEqual(r["diverged_old_m"], 0)
        mid = ((35.01 + 134.98) / 2, (135.0 + 34.58) / 2)
        r = compare([mid, (mid[0] + 0.0001, mid[1])], new, 20, 50, 500)
        self.assertEqual(r["diverged_old_m"], 0)  # the old line lies on the long segment

    def test_far_away_is_capped(self):
        r = compare(north(11), north(11, lon=136.0), 20, 50, 500)
        self.assertEqual((r["max_m"], r["capped"]), (500, True))


if __name__ == "__main__":
    unittest.main()
