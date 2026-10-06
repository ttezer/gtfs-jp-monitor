"""Compare two semantic engine configurations on a fixed sample of publication pairs
(docs/semantic/02-matching.md §8).

Every pair of the sample is reported under a base and a candidate configuration. The result keeps
a few counts per pair, their totals, the pairs whose line relations differ and the checks of pairs
with known expectations, so a setting can be measured before it is switched on. ZIPs are kept in a
local cache outside the repositories; a missing one is downloaded and must match the sample's
SHA-256.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from gtfs_jp_semantic.reader import Config
from gtfs_jp_semantic.report import LINE_STATUS_ORDER, build_report

from .download import download_zip
from .gtfsdatajp import BASE_URL
from .ids import is_path_id, is_uid

SAMPLE_SCHEMA = "gtfs-jp-monitor-semantic-sample/1"
RESULT_SCHEMA = "gtfs-jp-monitor-semantic-compare/1"
COUNTS = ("places_added", "places_removed", "places_renamed", "places_moved", "trip_moves", "pattern_edits",
          "unclassified", "unmatched_lines", "lines_changed")


def load_sample(path: Path) -> list[dict]:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if doc.get("schema") != SAMPLE_SCHEMA:
        raise ValueError(f"{path}: unexpected schema {doc.get('schema')!r}")
    for p in doc["pairs"]:
        org_id, _, feed_id = p["feed"].partition("/")
        if not (is_path_id(org_id) and is_path_id(feed_id)):
            raise ValueError(f"{path}: invalid feed {p['feed']!r}")
        for side in ("old", "new"):
            if not is_uid(p[side]["uid"]) or len(p[side]["sha256"]) != 64:
                raise ValueError(f"{path}: invalid {side} publication in {p['feed']}")
    return doc["pairs"]


def apply_override(config: Config, override: dict) -> Config:
    """`config` with the values of `override`; the matching and report sections are merged key by key."""
    fields = {f.name for f in dataclasses.fields(Config)}
    unknown = set(override) - fields
    if unknown:
        raise ValueError(f"unknown configuration fields: {sorted(unknown)}")
    changes = {k: v for k, v in override.items() if k not in ("matching", "report")}
    for section in ("matching", "report"):
        if section in override:
            changes[section] = {**getattr(config, section), **override[section]}
    return dataclasses.replace(config, **changes)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zip_path(zip_dir: Path, feed: str, pub: dict, download=download_zip) -> Path:
    """The cached ZIP of a publication, downloaded when missing; its SHA-256 must be the sample's."""
    path = Path(zip_dir) / f"{pub['uid']}.zip"
    if not path.exists():
        org_id, feed_id = feed.split("/")
        url = f"{BASE_URL}/organizations/{org_id}/feeds/{feed_id}/files/feed.zip?uid={pub['uid']}"
        tmp = path.with_suffix(".part")
        got = download(url, tmp)
        if got.sha256 != pub["sha256"]:
            tmp.unlink(missing_ok=True)
            raise ValueError(f"{feed} {pub['uid']}: downloaded ZIP does not match the sample's SHA-256")
        tmp.rename(path)
    elif _sha256(path) != pub["sha256"]:
        raise ValueError(f"{feed} {pub['uid']}: cached ZIP does not match the sample's SHA-256")
    return path


def measure(report: dict) -> dict:
    s = report["summary"]
    lines = {k: s["lines"][k] for k in LINE_STATUS_ORDER}
    return {
        "lines": lines,
        "places_added": s["places"]["added"], "places_removed": s["places"]["removed"],
        "places_renamed": s["places"]["renamed"], "places_moved": s["places"]["moved"],
        "trip_moves": s["trip_moves"],
        "pattern_edits": sum(len(p["edits"]) for line in report["lines"] for p in line["patterns"]),
        "unclassified": report["header"]["coverage"]["unclassified"],
        "unmatched_lines": lines["added"] + lines["discontinued"],
        "lines_changed": lines["changed"],
        "relations": sorted([line["status"], line["key"]] for line in report["lines"]),
    }


def check_expect(m: dict, expect: dict) -> list[str]:
    """Failures of a measurement against a pair's expectation (exact line counts, status per line key)."""
    out = []
    want = {k: expect.get("lines", {}).get(k, 0) for k in LINE_STATUS_ORDER}
    if "lines" in expect and m["lines"] != want:
        out.append(f"lines {m['lines']} != {want}")
    status = {key: st for st, key in m["relations"]}
    for key, st in expect.get("line_status", {}).items():
        if status.get(key) != st:
            out.append(f"line {key}: {status.get(key)} != {st}")
    return out


def run_pair(pair: dict, zip_dir: Path, configs: dict[str, Config]) -> dict:
    org_id, feed_id = pair["feed"].split("/")
    out = {"feed": pair["feed"], "group": pair["group"], "old": pair["old"]["uid"], "new": pair["new"]["uid"]}
    try:
        paths = [zip_path(zip_dir, pair["feed"], pair[side]) for side in ("old", "new")]
        for name, config in configs.items():
            report, _ = build_report(*paths, config=config, feed={"org_id": org_id, "feed_id": feed_id},
                                     old_pub=pair["old"], new_pub=pair["new"])
            m = measure(report)
            if "expect" in pair:
                m["expect_failures"] = check_expect(m, pair["expect"])
            out[name] = m
    except Exception as err:  # one pair failing must not end the comparison
        out["error"] = f"{type(err).__name__}: {err}"
    return out


def _relations(m: dict) -> list[list[str]]:
    """Line relations without the content status: a same-name line that changed or not is "same"."""
    return sorted(["same" if st in ("changed", "unchanged") else st, key] for st, key in m["relations"])


def summarise(results: list[dict], names=("base", "candidate")) -> dict:
    ok = [r for r in results if "error" not in r]
    totals = {n: {c: sum(r[n][c] for r in ok) for c in COUNTS} for n in names}
    changed = []
    for r in ok:
        a, b = _relations(r[names[0]]), _relations(r[names[1]])
        if a != b:
            changed.append({k: r[k] for k in ("feed", "group", "old", "new")}
                           | {"unmatched_lines": [r[names[0]]["unmatched_lines"], r[names[1]]["unmatched_lines"]],
                              "only_base": [x for x in a if x not in b], "only_candidate": [x for x in b if x not in a]})
    expect = [{k: r[k] for k in ("feed", "old", "new")} | {n: r[n]["expect_failures"] for n in names}
              for r in ok if "expect_failures" in r[names[0]]]
    return {"pairs": len(results), "errors": [{k: r[k] for k in ("feed", "old", "new", "error")}
                                             for r in results if "error" in r],
            "totals": totals, "line_relations_changed": changed, "expectations": expect}


def compare(pairs: list[dict], zip_dir: Path, base: Config, candidate: Config, jobs: int = 1) -> dict:
    configs = {"base": base, "candidate": candidate}
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(run_pair, pairs, [zip_dir] * len(pairs), [configs] * len(pairs)))
    else:
        results = [run_pair(p, zip_dir, configs) for p in pairs]
    return {"schema": RESULT_SCHEMA,
            "configs": {n: c.as_dict() for n, c in configs.items()},
            "summary": summarise(results), "pairs": results}


def summary_text(summary: dict) -> str:
    t = summary["totals"]
    lines = [f"{summary['pairs']} pairs, {len(summary['errors'])} errors",
             f"{'':16}{'base':>10}{'candidate':>11}{'delta':>9}"]
    for c in COUNTS:
        b, a = t["base"][c], t["candidate"][c]
        lines.append(f"{c:16}{b:>10}{a:>11}{a - b:>+9}")
    ch = summary["line_relations_changed"]
    lines.append(f"line relations changed in {len(ch)} pairs "
                 f"(unmatched lines fewer in {sum(1 for x in ch if x['unmatched_lines'][1] < x['unmatched_lines'][0])}, "
                 f"more in {sum(1 for x in ch if x['unmatched_lines'][1] > x['unmatched_lines'][0])})")
    for e in summary["expectations"]:
        verdict = lambda f: "ok" if not f else "FAIL " + "; ".join(f)
        lines.append(f"expectation {e['feed']} {e['old'][:8]}→{e['new'][:8]}: "
                     f"base {verdict(e['base'])}, candidate {verdict(e['candidate'])}")
    for e in summary["errors"]:
        lines.append(f"error {e['feed']} {e['old'][:8]}→{e['new'][:8]}: {e['error']}")
    return "\n".join(lines)
