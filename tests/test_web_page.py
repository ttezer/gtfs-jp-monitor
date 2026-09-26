import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "web" / "prototype" / "index.html"


class WebPageTest(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text(encoding="utf-8")

    def test_one_export_placeholder(self):
        self.assertEqual(self.html.count("/*__EXPORT__*/"), 1)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_script_parses(self):
        # The page script is written by hand; a syntax error would leave the whole site blank.
        script = self.html[self.html.rindex("<script>") + len("<script>"):self.html.rindex("</script>")]
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
            f.write(script)
        try:
            proc = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        finally:
            Path(f.name).unlink()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_pair_indices(self):
        # A report can be opened for any two groups the page shows, not only neighbours.
        start = self.html.index("function pairIndices(")
        source = self.html[start:self.html.index("\n}\n", start) + 3]
        cases = """
        const gs = [["a"], ["b", "c"], ["d"], ["e"]].map(ids => ({ members: ids.map(uid => ({ uid })) }));
        const out = [pairIndices(gs, "c__d"), pairIndices(gs, "a__e"), pairIndices(gs, "a__b"), pairIndices(gs, "x__e")];
        console.log(JSON.stringify(out));
        """
        proc = subprocess.run(["node", "-e", source + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # neighbours; non-neighbours; old inside a merged group; unknown old uid falls back to the previous group
        self.assertEqual(json.loads(proc.stdout), [[1, 2], [0, 3], [0, 1], [2, 3]])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_skipped_between(self):
        start = self.html.index("function skippedBetween(")
        source = self.html[start:self.html.index("\n}\n", start) + 3]
        cases = """
        const f = { generations: ["a", "b", "c", "d"].map((uid, i) => ({ uid, status: i === 2 ? "FATAL" : "COMPLETE" })) };
        console.log(JSON.stringify([skippedBetween(f, "a", "b"), skippedBetween(f, "a", "d"), skippedBetween(f, "x", "d")]));
        """
        proc = subprocess.run(["node", "-e", source + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [{"n": 0, "fatal": 0}, {"n": 2, "fatal": 1}, {"n": 0, "fatal": 0}])

    def functions(self, *names):
        """Source of top-level page functions, to run them in node without a browser."""
        out = []
        for name in names:
            start = self.html.index(f"function {name}(")
            out.append(self.html[start:self.html.index("\n}\n", start) + 3])
        return "\n".join(out)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_links(self):
        source = ("const TABS = [\"overview\", \"history\", \"reports\", \"gtfsjp\"], VIEWS = [\"report\", \"verify\"];\n"
                  "const key = f => `${f.org_id}/${f.feed_id}`;\n" + self.functions("parseLink", "resolveLink", "linkHash"))
        cases = """
        const gens = ["a", "b", "c", "d", "e"].map(uid => ({ uid }));
        const merged = [["a"], ["b", "c"], ["d"], ["e"]].map(ids => ({ rep: { uid: ids[ids.length - 1] }, members: ids.map(uid => ({ uid })) }));
        const single = gens.map(g => ({ rep: g, members: [g] }));
        const f = { org_id: "org", feed_id: "feed-1" };
        console.log(JSON.stringify([
          parseLink("#feed=org/feed-1&old=a&new=d&tab=reports&view=verify"),
          parseLink("#feed=org%2Ffeed-1&tab=nope&view=x"),
          resolveLink(gens, merged, "a", "d"), resolveLink(gens, merged, "d", "a"),   // reversed link
          resolveLink(gens, merged, "c", "e"), resolveLink(gens, single, "c", "e"),  // uid inside a merged group
          resolveLink(gens, merged, "a", "zz"),
          linkHash(f, merged, 3, 1, "reports", "verify"), linkHash(f, merged, 0, 1, "overview", "report"), linkHash(null, null, null, null, "overview", "report"),
        ]));
        """
        proc = subprocess.run(["node", "-e", source + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            {"feed": "org/feed-1", "old": "a", "new": "d", "tab": "reports", "view": "verify"},
            {"feed": "org/feed-1", "old": None, "new": None, "tab": None, "view": None},
            [0, 2], [0, 2], [1, 3], [2, 4], None,
            "#feed=org/feed-1&old=c&new=e&tab=reports&view=verify",  # old side is the last uid of its group
            "#feed=org/feed-1&old=a&new=b", "",
        ])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_status_note(self):
        start = self.html.index("const T = {")
        texts = self.html[start:self.html.index("\n};\n", start) + 3]
        source = texts + "const STALE_HOURS = 36;\n" + self.functions("statusNote")
        cases = """
        const built = Date.parse("2026-09-26T19:30:00Z");
        const st = { built_at: "2026-09-26T19:30:00Z", backlog: { unanalysed: 12, reports_pending: 0 } };
        console.log(JSON.stringify([
          statusNote(st, "en", built + 5 * 3600000), statusNote({ ...st, backlog: {} }, "tr", built + 40 * 3600000),
          statusNote(st, "ja", built + 3 * 86400000), statusNote(null, "en", built)]));
        """
        proc = subprocess.run(["node", "-e", source + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            "Last updated 5 hours ago (2026-09-27 04:30 JST). 12 older publications waiting for analysis, 0 pairs waiting for a report.",
            "Son güncelleme: 40 saat önce (2026-09-27 04:30 JST). Veriler 40 saattir güncellenmedi; günlük koşu aksamış olabilir.",
            "最終更新：3日前（2026-09-27 04:30 JST）。 72時間更新がありません。毎日の実行が止まっている可能性があります。 解析待ちの古い公開 12 件、レポート待ちのペア 0 件。",
            "",
        ])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_report_search(self):
        cases = """
        const items = ["熊本電鉄 kumamoto 2026-04-01", "三重交通 sanco 2025-10-01", "熊本都市バス toshibus 2025-10-01"].map(text => ({ text: text.toLowerCase() }));
        const n = q => reportMatches(items, q).length;
        console.log(JSON.stringify([n(""), n("熊本"), n("2025-10"), n("Kumamoto 2026"), n("nothing")]));
        """
        proc = subprocess.run(["node", "-e", self.functions("reportMatches") + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [3, 2, 2, 1, 0])  # every word must match, case-insensitive

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_travel_times_and_band_shares(self):
        source = ("const firstTime = times => times.find(x => x != null);\n"
                  "const lastTime = times => { for (let i = times.length - 1; i >= 0; i--) if (times[i] != null) return times[i]; return null; };\n"
                  "const median = xs => { if (!xs.length) return null; const v = xs.slice().sort((a, b) => a - b), m = v.length >> 1; return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2; };\n"
                  + self.functions("travelTimes", "bandShares"))
        cases = """
        const tab = { old: { trips: [{ times: [480, 490, 500] }, { times: [600, null, 630] }] },
                      new: { trips: [{ times: [480, 495, 510] }, { times: [600, 610, 630] }, { times: [700, 720] }] },
                      pairs: [[0, 0], [1, 1], [null, 2]] };
        const lines = [{ trips: [{ bands: { "07-08": { before: 2, after: 1 }, "08-09": { before: 2, after: 3 } } }] }];
        console.log(JSON.stringify([travelTimes(tab), bandShares(lines).map(r => [r.band, Math.round(r.d)])]));
        """
        proc = subprocess.run(["node", "-e", source + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            {"old": 25, "new": 30, "longer": 1, "shorter": 0, "same": 1},  # medians of [20, 30] and [30, 30, 20]
            [["07-08", -25], ["08-09", 25]],
        ])


if __name__ == "__main__":
    unittest.main()
