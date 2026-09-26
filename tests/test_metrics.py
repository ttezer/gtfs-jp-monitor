import datetime as dt
import unittest

from gtfs_jp_monitor.metrics import build_metrics


def gen(uid, day, pair=None, status="COMPLETE", publish=100.0, rules=None, fmt=False):
    g = {"uid": uid, "published_at": f"2026-{day}T10:00:00+09:00", "status": status, "scores": {"publish": publish},
         "rules": rules or {}}
    if pair:
        g["pair"] = pair
    if fmt:
        g["format_transition"] = True
    return g


class MetricsTest(unittest.TestCase):
    def test_counts_ratios_and_coverage(self):
        gens = [gen("a", "01-10"), gen("b", "02-10", "M"), gen("c", "03-10", "T", publish=90.0),
                gen("d", "04-10", "E"), gen("e", "05-10", "U/NOT_REPORTED", rules={"X": [1, "HIGH", "SPEC"]}),
                gen("f", "06-10", "M", rules={"X": [1, "HIGH", "SPEC"], "Y": [2, "CRITICAL", "SPEC"]}, fmt=True)]
        feeds = [{"org_id": "o", "feed_id": "f", "generations": gens}]
        catalog = {("o", "f"): {g["uid"]: {"published_at": g["published_at"], "present": g["uid"] != "a"} for g in gens}}
        index = {"a__b": {"coverage": {"unclassified": 1, "raw_total": 100}}, "b__c": {"coverage": {"unclassified": 0, "raw_total": 100}}}
        m = build_metrics(feeds, catalog, index, "v0.14.0__auto", "0.5.0", dt.date(2026, 9, 26))
        self.assertEqual((m["from"], m["to"], m["analysis_key"], m["engine_version"]), ("2025-09-26", "2026-09-26", "v0.14.0__auto", "0.5.0"))
        f = m["feeds"]["o/f"]
        self.assertEqual(f["counts"], {"meaningful": 2, "technical": 1, "equivalent": 1, "unknown": 1,
                                        "estimated_meaningful": 0, "estimated_technical": 0,
                                        "regressions": 2, "compared": 4})  # c: score fell; e: HIGH rule appeared; f skipped (format)
        self.assertEqual((f["publications"], f["pairs"], f["coverage"]), (6, 5, 0.8))
        self.assertEqual((f["equivalent_republication_ratio"], f["technical_only_change_ratio"]), (0.25, 0.25))
        self.assertEqual((f["unknown_classification_ratio"], f["unclassified_diff_ratio"]), (0.2, 0.005))
        self.assertEqual((f["source_availability_rate"], f["meaningful_update_frequency"]), (round(5 / 6, 4), round(2 / (365 / 30), 2)))
        self.assertEqual(m["total"]["counts"], f["counts"])

    def test_estimates_count_as_known_but_apart(self):
        gens = [gen("a", "01-10"), gen("b", "02-10", "~T"), gen("c", "03-10", "~M"), gen("d", "04-10", "M"), gen("e", "05-10", "U/NOT_REPORTED")]
        m = build_metrics([{"org_id": "o", "feed_id": "f", "generations": gens}], {}, {}, "k", "0.5.0", dt.date(2026, 9, 26))["total"]
        self.assertEqual((m["counts"]["estimated_technical"], m["counts"]["estimated_meaningful"], m["counts"]["meaningful"]), (1, 1, 1))
        self.assertEqual((m["coverage"], m["exact_coverage"], m["technical_only_change_ratio"]), (0.75, 0.25, round(1 / 3, 4)))
        self.assertEqual(m["meaningful_update_frequency"], round(2 / (365 / 30), 2))

    def test_window(self):
        gens = [gen("a", "01-10"), gen("b", "02-10", "M")]
        feeds = [{"org_id": "o", "feed_id": "f", "generations": gens}]
        m = build_metrics(feeds, {}, {}, "k", "0.5.0", dt.date(2027, 3, 1))  # b is older than 365 days
        self.assertEqual((m["feeds"]["o/f"]["pairs"], m["feeds"]["o/f"]["coverage"]), (0, None))


if __name__ == "__main__":
    unittest.main()
