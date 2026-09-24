import tempfile
import unittest
from pathlib import Path

from gtfs_jp_monitor.ids import (
    InvalidIdentifier,
    feed_dir,
    is_path_id,
    is_uid,
    require_uid,
    rid_rank,
)
from gtfs_jp_monitor.licenses import normalize_license
from gtfs_jp_monitor.ordering import GenerationRef, order_generations, rid_order_matches


def uid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


class IdsTest(unittest.TestCase):
    def test_path_ids(self):
        for ok in ("nagai-unyu", "Nagaibus", "GTFS-Tosacity_Bus", "a.b"):
            self.assertTrue(is_path_id(ok), ok)
        for bad in ("", ".", "..", "../x", "a/b", "a\\b", "-lead", " x", "x" * 129, None, 5):
            self.assertFalse(is_path_id(bad), bad)

    def test_uids(self):
        self.assertTrue(is_uid("17ab34e1-dcb8-4b2e-9cda-ae2b68f4c444"))
        for bad in ("17AB34E1-DCB8-4B2E-9CDA-AE2B68F4C444", "17ab34e1-dcb8-1b2e-9cda-ae2b68f4c444", "x", None):
            self.assertFalse(is_uid(bad), bad)
        with self.assertRaises(InvalidIdentifier):
            require_uid("../../etc/passwd")

    def test_rid_rank(self):
        self.assertEqual([rid_rank(r) for r in ("next_2", "next_1", "current", "prev_1", "prev_69")], [-2, -1, 0, 1, 69])
        for bad in ("prev_0", "prev_", "latest", "next-1", ""):
            with self.subTest(bad=bad), self.assertRaises(InvalidIdentifier):
                rid_rank(bad)

    def test_feed_dir_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(feed_dir(Path(tmp), "nagai-unyu", "Nagaibus"), Path(tmp) / "feeds" / "nagai-unyu" / "Nagaibus")
            with self.assertRaises(InvalidIdentifier):
                feed_dir(Path(tmp), "..", "Nagaibus")


class OrderingTest(unittest.TestCase):
    def test_from_date_leads_even_when_published_at_disagrees(self):
        # Nagaibus: old generations were bulk re-uploaded on 2023-10-14.
        older = GenerationRef(uid(1), "2018-04-01", "2023-10-14T17:42:42.002187+09:00", "prev_69")
        newer = GenerationRef(uid(2), "2023-02-07", "2023-03-10T13:39:38.988054+09:00", "prev_22")
        ordered, unknown = order_generations([newer, older])
        self.assertEqual([g.uid for g in ordered], [uid(1), uid(2)])
        self.assertEqual(unknown, [])
        self.assertTrue(rid_order_matches(ordered))

    def test_same_from_date_orders_by_published_at(self):
        a = GenerationRef(uid(9), "2023-10-01", "2023-09-08T12:45:24.087402+09:00", "prev_15")
        b = GenerationRef(uid(1), "2023-10-01", "2023-10-05T13:30:08.617900+09:00", "prev_14")
        ordered, _ = order_generations([b, a])
        self.assertEqual([g.uid for g in ordered], [uid(9), uid(1)])

    def test_published_at_parsed_not_compared_as_text(self):
        # "…26+09:00" < "…26.5+09:00" as instants, but not as strings ('+' < '.').
        a = GenerationRef(uid(2), "2026-04-01", "2026-07-08T18:09:26+09:00")
        b = GenerationRef(uid(1), "2026-04-01", "2026-07-08T18:09:26.517446+09:00")
        ordered, _ = order_generations([b, a])
        self.assertEqual([g.uid for g in ordered], [uid(2), uid(1)])

    def test_offsets_are_compared_as_instants(self):
        a = GenerationRef(uid(2), "2026-04-01", "2026-07-08T09:00:00+00:00")  # 18:00 JST
        b = GenerationRef(uid(1), "2026-04-01", "2026-07-08T17:00:00+09:00")
        ordered, _ = order_generations([a, b])
        self.assertEqual([g.uid for g in ordered], [uid(1), uid(2)])

    def test_uid_breaks_full_ties(self):
        a = GenerationRef(uid(2), "2026-04-01", "2026-03-01T00:00:00+09:00")
        b = GenerationRef(uid(1), "2026-04-01", "2026-03-01T00:00:00+09:00")
        ordered, _ = order_generations([a, b])
        self.assertEqual([g.uid for g in ordered], [uid(1), uid(2)])

    def test_missing_published_at_sorts_first_within_day(self):
        a = GenerationRef(uid(1), "2026-04-01", "2026-03-01T00:00:00+09:00")
        b = GenerationRef(uid(2), "2026-04-01", None)
        ordered, _ = order_generations([a, b])
        self.assertEqual([g.uid for g in ordered], [uid(2), uid(1)])

    def test_missing_from_date_is_unorderable(self):
        a = GenerationRef(uid(1), None, "2026-03-01T00:00:00+09:00")
        b = GenerationRef(uid(2), "not-a-date", None)
        c = GenerationRef(uid(3), "2026-04-01", None)
        ordered, unknown = order_generations([a, b, c])
        self.assertEqual([g.uid for g in ordered], [uid(3)])
        self.assertEqual([g.uid for g in unknown], [uid(1), uid(2)])

    def test_next_generations_are_newest(self):
        cur = GenerationRef(uid(1), "2026-04-01", "2026-03-28T00:00:00+09:00", "current")
        nxt = GenerationRef(uid(2), "2026-10-01", "2026-09-15T00:00:00+09:00", "next_1")
        ordered, _ = order_generations([nxt, cur])
        self.assertEqual([g.uid for g in ordered], [uid(1), uid(2)])
        self.assertTrue(rid_order_matches(ordered))

    def test_rid_mismatch_is_detected(self):
        a = GenerationRef(uid(1), "2025-01-01", None, "current")
        b = GenerationRef(uid(2), "2026-01-01", None, "prev_1")
        ordered, _ = order_generations([a, b])
        self.assertFalse(rid_order_matches(ordered))


class LicenseTest(unittest.TestCase):
    def test_known_values(self):
        self.assertEqual(normalize_license("CC BY 4.0"), "CC-BY-4.0")
        self.assertEqual(normalize_license("CC0 1.0"), "CC0-1.0")
        self.assertEqual(normalize_license("CC BY 2.1 JP"), "CC-BY-2.1-JP")
        self.assertEqual(normalize_license("  CC BY   4.0 "), "CC-BY-4.0")

    def test_ambiguous_or_unknown(self):
        for raw in ("CC-BY", "", None, "MIT"):
            self.assertIsNone(normalize_license(raw), raw)


if __name__ == "__main__":
    unittest.main()
