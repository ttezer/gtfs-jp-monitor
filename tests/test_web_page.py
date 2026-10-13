import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gtfs_jp_monitor import webexport
from gtfs_jp_monitor.diff import build_diff

from .test_diff_builder import gen_doc

PAGE = Path(__file__).resolve().parent.parent / "web" / "prototype" / "index.html"


class WebPageTest(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text(encoding="utf-8")

    def test_one_export_placeholder(self):
        self.assertEqual(self.html.count("/*__EXPORT__*/"), 1)

    def test_site_head(self):
        head = '<title>GTFS-FR Monitor</title>\n<link rel="alternate" type="application/atom+xml" hreflang="fr" href="atom/fr/all.xml">\n'
        page = webexport.apply_site_head(self.html, head)
        self.assertIn(head, page)
        self.assertNotIn("GTFS-JP Monitor</title>", page)
        self.assertNotIn("googletagmanager", page)  # the analytics belong to this site's head only
        self.assertEqual(page.count("/*__EXPORT__*/"), 1)
        with self.assertRaises(ValueError):
            webexport.apply_site_head(page, head)  # no markers left

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_languages_have_the_same_texts(self):
        # A text missing in one language shows as "undefined" on the page.
        block = lambda name: self.html[self.html.index(f"const {name} = {{"):self.html.index("\n};\n", self.html.index(f"const {name} = {{")) + 3]
        js = block("T") + block("R") + """
const shape = o => Object.fromEntries(Object.entries(o).map(([k, v]) =>
  [k, v && typeof v === "object" && !Array.isArray(v) ? shape(v) : Array.isArray(v) ? v.length : typeof v]));
const out = {};
for (const [name, table] of [["T", T], ["R", R]])
  for (const lang of Object.keys(table)) out[`${name}.${lang}`] = shape(table[lang]);
console.log(JSON.stringify(out));"""
        proc = subprocess.run(["node", "-e", "const state = {};" + js], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        shapes = json.loads(proc.stdout)
        for name in ("T", "R"):
            for lang in ("tr", "ja", "fr"):
                with self.subTest(table=name, lang=lang):
                    self.assertEqual(shapes[f"{name}.{lang}"], shapes[f"{name}.en"])

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
        const withFatal = [["a"], ["F"], ["c"]].map(ids => ({ members: ids.map(uid => ({ uid, status: uid === "F" ? "FATAL" : "COMPLETE" })) }));
        const out = [pairIndices(gs, "c__d"), pairIndices(gs, "a__e"), pairIndices(gs, "a__b"), pairIndices(gs, "x__e"), pairIndices(withFatal, "a__c")];
        console.log(JSON.stringify(out));
        """
        proc = subprocess.run(["node", "-e", source + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # neighbours; non-neighbours; old inside a merged group; unknown old uid falls back to the previous group
        # ...and a pair across a FATAL publication opens on its own two groups
        self.assertEqual(json.loads(proc.stdout), [[1, 2], [0, 3], [0, 1], [2, 3], [0, 2]])

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

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_comparator_state(self):
        cases = """
        const g = (uid, extra) => Object.assign({ uid, status: "COMPLETE", method: "m" }, extra);
        const f = { generations: [g("a"), g("b", { cmp: "SAME_FEED" }), g("c", { cmp: "COMPARABLE" }), g("d", { method: "x" }), g("e", { status: "PARTIAL" })] };
        const [a, b, c, d, e] = f.generations;
        console.log(JSON.stringify([comparatorState(f, a, b), comparatorState(f, b, a), comparatorState(f, a, c),
                                    comparatorState(f, b, c), comparatorState(f, c, d), comparatorState(f, a, e)]));
        """
        proc = subprocess.run(["node", "-e", self.functions("comparatorState") + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # same file only known for neighbours (a, c are two apart)
        self.assertEqual(json.loads(proc.stdout), ["SAME_FEED", "SAME_FEED", "COMPARABLE", "COMPARABLE", "INCOMPATIBLE_METHOD",
                                                   "UNVERIFIED_INPUT"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_impact_summary(self):
        start = self.html.index("const R = {")
        texts = self.html[start:self.html.index("\n};\n", start) + 3]
        start = self.html.index("const IMP_ORDER =")
        impact = self.html[start:self.html.index("const pname =", start)]
        source = (texts + "const state = { lang: \"en\" };\n"
                  + "const esc = s => String(s ?? \"\");\n"
                  + "".join(self.line(p) for p in ("const rt =", "const CATS =", "const LEGACY_DT =", "const dtCats =",
                                                   "const byDt =", "const dayTypes ="))
                  + self.functions("dtLabel") + impact)
        cases = """
        const imp = { class: "major",
          types: { ROUTE_MERGED: { major: 2 }, STOP_ADDED: { major: 1, technical: 3 }, STOP_RENAMED: { minor: 1 } },
          lines: { A: { class: "major", types: { ROUTE_MERGED: 1, PATTERN_EXTENDED: 1 } }, B: { class: "minor", types: { SERVICE_REDUCED: 2 } },
                   C: { class: "technical", types: { SERVICE_REDUCED: 1 } } },
          trips: { "mon,tue,wed,thu,fri": { added: 0, removed: 5, retimed: 3 } }, service_lines: {} };
        const rep = { impact: imp };
        console.log(JSON.stringify([impactSummary(imp), impactSummary(null), lineImpact(rep, { key: "A" }), lineImpact(rep, { key: "Z" }),
          impactSummary({ class: "technical", types: {}, lines: {}, trips: {}, service_lines: {} })]));
        """
        proc = subprocess.run(["node", "-e", source + cases], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), [
            # technical changes (3 stops no line serves, line C) are left out
            '<p class="impsum"><span class="imp imp-major">Major impact</span> 2 line mergers, 1 stop added, 1 stop renamed, '
            'route changed on 1 line, fewer trips on 1 line, Mon–Fri: 5 trips removed</p>',
            "",
            '<span class="imp imp-major">Major impact</span><span class="lt">merged · route extended</span>',
            "",
            '<p class="impsum"><span class="imp imp-technical">Technical</span> No passenger-facing change.</p>',
        ])

    def line(self, prefix):
        """One top-level line of the page script, such as a `const` helper."""
        start = self.html.index(prefix)
        return self.html[start:self.html.index("\n", start) + 1]

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_diff_matches_build_diff(self):
        # The page compares any two publications itself (data-model §9); it must give the same
        # rule buckets and score/metric deltas as `build_diff`, after the export has compacted
        # the rules and the page has expanded them again.
        docs = [gen_doc(1, **{"scores.publish": 96.2, "scores.spec": 99.9, "scores.interop": 0.0,
                              "metrics.routes": 22, "metrics.avg_daily_trips": 0.1}),
                gen_doc(2, **{"scores.publish": 98.1, "scores.spec": 100.0, "scores.interop": 70.7,
                              "metrics.avg_daily_trips": 0.3, "metrics.trips": 0}),
                gen_doc(3, **{"scores.quality": 80.0, "metrics.stops": 390})]
        docs[0]["rules"] = {"CAL_013": rule(3, "LOW"), "STP_037": rule(12, "LOW"), "FRQ_002": rule(1, "INFO"),
                            "AAA_001": rule(1, "INFO"), "SEV_001": rule(2, "LOW")}
        docs[1]["rules"] = {"JPN_030": rule(4, "MEDIUM"), "STP_037": rule(5, "LOW"), "FRQ_002": rule(1, "INFO"),
                            "AAA_001": rule(2, "INFO"), "SEV_001": rule(2, "LOW")}
        docs[2]["rules"] = {"CAL_013": rule(1, "LOW"), "SEV_001": rule(5, "HIGH")}  # severity differs: kept in full
        feed = {"generations": [webexport._summary({"uid": d["generation"]["uid"], "from_date": "2026-04-01",
                                                    "to_date": "2027-03-31", "published_at": None}, d) for d in docs]}
        meta = webexport.compact_rules([feed])
        self.assertIsInstance(feed["generations"][0]["rules"]["SEV_001"], int)
        self.assertIsInstance(feed["generations"][2]["rules"]["SEV_001"], list)
        pairs = [(0, 1), (1, 0), (1, 2), (0, 2)]
        start = self.html.index("if (DATA.rule_meta)")
        expand = self.html[start:self.html.index("\n}\n", start) + 3]
        source = ("const DATA = " + json.dumps({"rule_meta": meta, "feeds": [feed]}) + ";\n" + expand
                  + self.line("const SCORES =") + self.line("const METRICS =")
                  + self.line("const round1 =") + self.line("const delta1 =") + self.functions("diff")
                  + "const g = DATA.feeds[0].generations;\n"
                  + f"console.log(JSON.stringify({json.dumps(pairs)}.map(([o, n]) => diff(g[o], g[n]))));\n")
        proc = subprocess.run(["node", "-e", source], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for (o, n), page in zip(pairs, json.loads(proc.stdout)):
            with self.subTest(old=o + 1, new=n + 1):
                ref = build_diff(docs[o], docs[n])
                self.assertEqual(page["scores"], {s: v["delta"] for s, v in ref["scores"].items()})
                self.assertEqual(page["metrics"], {m: ref["metrics"][m]["delta"] for m in page["metrics"]})
                self.assertEqual(set(page["metrics"]), set(ref["metrics"]))
                self.assertEqual({b: {r["id"]: {"before": r["before"], "after": r["after"]} for r in rows}
                                  for b, rows in page["rules"].items()}, ref["rules"])
                # severity shown is the newer side's, or the older side's for a fixed rule
                for rows in page["rules"].values():
                    for r in rows:
                        side = docs[n]["rules"].get(r["id"]) or docs[o]["rules"][r["id"]]
                        self.assertEqual(r["sev"], side["severity"], r["id"])


def rule(count, severity):
    return {"count": count, "severity": severity, "class": "QUALITY"}


if __name__ == "__main__":
    unittest.main()
