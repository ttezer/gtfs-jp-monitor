"""Compact export of the data repository for the web page (prototype of the public export).

Language-neutral data plus per-language rule titles taken from the analyzer's own catalog.
Validation diffs are not exported: the page computes them from generation summaries with the
same rules as `diff.build_diff` (data-model §9). Semantic change reports are indexed in the
export and written one file per pair, which the page loads on demand.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import json
import subprocess
from pathlib import Path

from gtfs_jp_semantic import ENGINE_VERSION

from .canonical import write_json
from .catalog import load_catalog
from .classify import EQUIVALENT, MEANINGFUL, TECHNICAL, classify_report, load_rules
from .ids import feed_dir
from .metrics import build_metrics
from .ordering import GenerationRef, order_generations
from .store import content_digest, generation_path, list_analyses, load_content

SCHEMA = "gtfs-jp-monitor-web-export/1"
LANGS = ("tr", "en", "ja")
JP_FILES = frozenset({"agency_jp.txt", "office_jp.txt", "routes_jp.txt", "pattern_jp.txt"})


def rule_titles(analyzer: Path, rule_ids: set[str], timeout: float = 60) -> dict[str, dict[str, str]]:
    """{lang: {rule_id: title}} from `gtfs-analyzer rules --json --lang <lang>`."""
    titles: dict[str, dict[str, str]] = {}
    for lang in LANGS:
        proc = subprocess.run([str(analyzer), "rules", "--json", "--lang", lang],
                              capture_output=True, timeout=timeout, check=True)
        rules = json.loads(proc.stdout)
        titles[lang] = {r["id"]: r["title"] for r in rules if r.get("id") in rule_ids and isinstance(r.get("title"), str)}
    return titles


def _summary(entry: dict, doc: dict) -> dict:
    m = doc["metrics"] or {}
    return {
        "uid": entry["uid"],
        "rid": entry.get("rid_observed"),
        "from_date": entry["from_date"],
        "to_date": entry["to_date"],
        "published_at": entry["published_at"],
        "memo": entry.get("memo", ""),
        "status": doc["validation_status"],
        "publishable": doc["publishable"],
        "scores": doc["scores"],
        "metrics": {k: m.get(k) for k in ("routes", "stops", "trips", "shapes", "active_service_days", "avg_daily_trips")}
        if m else None,
        # [count, severity, class] keeps the file small.
        "rules": {rid: [r["count"], r["severity"], r["class"]] for rid, r in sorted((doc["rules"] or {}).items())},
        # GTFS-JP extension files present; shown as a fact, not as a version claim.
        "jp_files": sorted(n for n in (doc["file_row_counts"] or {}) if n in JP_FILES),
    }


def _version(name: str) -> tuple[int, ...] | None:
    parts = name.split(".")
    return tuple(int(p) for p in parts) if len(parts) == 3 and all(p.isdigit() for p in parts) else None


def report_files(root: Path, feeds: list[dict], engine_version: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Semantic reports of exported feeds: a small index for the page and one file per report,
    {"<org_id>/<feed_id>/<old_uid>__<new_uid>": report}, loaded on demand.

    Each pair uses the newest engine version stored for it, up to `engine_version`, so the site
    keeps older reports while a new engine version is rebuilding them."""
    index: dict[str, dict] = {}
    files: dict[str, dict] = {}
    limit = _version(engine_version)
    rules = load_rules()
    for f in feeds:
        base = feed_dir(root, f["org_id"], f["feed_id"]) / "changes"
        if not base.is_dir():
            continue
        versions = sorted((v for d in base.iterdir() if d.is_dir() and (v := _version(d.name)) and v <= limit), reverse=True)
        newest: dict[str, Path] = {}
        for v in versions:
            for path in sorted((base / ".".join(map(str, v))).glob("*.report.json.gz")):
                newest.setdefault(path.name, path)
        for _, path in sorted(newest.items()):
            doc = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
            h = doc["header"]
            pair = f"{h['old']['uid']}__{h['new']['uid']}"
            files[f"{f['org_id']}/{f['feed_id']}/{pair}"] = doc
            index[pair] = {"feed": [f["org_id"], f["feed_id"]],
                           "dates": [h["old"]["from_date"], h["new"]["from_date"]],
                           "summary": doc["summary"], "coverage": h["coverage"],
                           "classification": classify_report(doc, rules)}
    return index, files


PAIR_CODES = {MEANINGFUL: "M", TECHNICAL: "T", EQUIVALENT: "E"}
# Without a report, a pair is estimated from which files differ (data-model §12): only these ->
# technical, anything else -> meaningful. On reported pairs this agrees with the report 99.5% of
# the time for the technical side (2026-09-27: 499 of 501) and 43 of 44 for the meaningful side.
ESTIMATE_TECHNICAL_FILES = frozenset({"feed_info.txt", "calendar.txt", "calendar_dates.txt"})


def estimate_pair(old: dict | None, new: dict | None) -> str | None:
    """"~T" or "~M" from two content records with per-file hashes, else None."""
    if not (old and new and old.get("files") and new.get("files")):
        return None
    a, b = old["files"], new["files"]
    changed = {n for n in set(a) | set(b) if (a.get(n) or {}).get("hash") != (b.get(n) or {}).get("hash")}
    return "~T" if changed <= ESTIMATE_TECHNICAL_FILES else "~M"


def classify_pairs(root: Path, feeds: list[dict], index: dict[str, dict], engine_version: str) -> None:
    """Give every exported publication after the first the class of the pair it forms with the
    publication before it (data-model §12) as a short code in `pair`: "M" meaningful, "T"
    technical, "E" equivalent, "~M" / "~T" estimated from the changed files where no report
    exists, or "U/<reason>" unknown ("U/FATAL", "U/NOT_REPORTED" or an error
    code of the report such as "U/SOURCE_UNAVAILABLE"). `format_transition` is true where the
    GTFS-JP extension files differ."""
    for f in feeds:
        gens = f["generations"]
        for prev, g in zip(gens, gens[1:]):
            key = f"{prev['uid']}__{g['uid']}"
            if "FATAL" in (prev["status"], g["status"]):
                code = "U/FATAL"
            elif g["equivalent_to_previous"]:
                code = PAIR_CODES[EQUIVALENT]
            elif key in index:
                code = PAIR_CODES[index[key]["classification"]["class"]]
            else:
                marker = feed_dir(root, f["org_id"], f["feed_id"]) / "changes" / engine_version / f"{key}.error.json"
                if marker.is_file():
                    code = "U/" + (json.loads(marker.read_text(encoding="utf-8")).get("code") or "ERROR")
                else:  # outside the reported window (§11) or not built yet: estimate if possible
                    code = estimate_pair(load_content(root, f["org_id"], f["feed_id"], prev["uid"]),
                                         load_content(root, f["org_id"], f["feed_id"], g["uid"])) or "U/NOT_REPORTED"
            g["pair"] = code
            if prev["jp_files"] != g["jp_files"]:
                g["format_transition"] = True


def _bytes(paths) -> tuple[int, int, int]:
    sizes = [p.stat().st_size for p in paths]
    return len(sizes), sum(sizes), max(sizes, default=0)


HISTORY_PATH = Path("status") / "storage-history.json"
GROWTH_DAYS = 30


def record_storage(root: Path, repo_kb: int | None, day: _dt.date, engine_version: str = ENGINE_VERSION) -> dict:
    """Add (or replace) today's sizes in status/storage-history.json of the data repository, the
    base of the growth rates in status.json (data-model §10.2). Returns the entry."""
    base = root / "feeds"
    entry = {"date": day.isoformat(), "engine_version": engine_version, "repo_kb": repo_kb,
             "data_bytes": _bytes(p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)[1],
             "reports_bytes": _bytes(base.glob("*/*/changes/*/*.report.json.gz"))[1]}
    path = root / HISTORY_PATH
    history = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    history = [h for h in history if h["date"] != entry["date"]] + [entry]
    write_json(path, sorted(history, key=lambda h: h["date"]))
    return entry


def growth(history: list[dict], today: _dt.date, days: int = GROWTH_DAYS) -> dict | None:
    """MB per day over the last `days` days of the storage history, from its first to its last
    entry in that span; None with fewer than two entries."""
    start = today - _dt.timedelta(days=days)
    span = [h for h in history if _dt.date.fromisoformat(h["date"]) >= start]
    if len(span) < 2:
        return None
    a, b = span[0], span[-1]
    n = (_dt.date.fromisoformat(b["date"]) - _dt.date.fromisoformat(a["date"])).days or 1
    rate = lambda k, unit: round((b[k] - a[k]) * unit / n / 2**20, 2) if a.get(k) is not None and b.get(k) is not None else None
    return {"from": a["date"], "to": b["date"], "repo_mb_per_day": rate("repo_kb", 1024),
            "data_mb_per_day": rate("data_bytes", 1), "reports_mb_per_day": rate("reports_bytes", 1)}


def read_stages(path: Path) -> dict:
    """{stage: {"started", "finished", "minutes"}} from lines "<stage> start|end <UTC time>" that the
    workflow writes around its steps; a stage without an end has only "started"."""
    stages: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] not in ("start", "end"):
            continue
        stages.setdefault(parts[0], {})["started" if parts[1] == "start" else "finished"] = parts[2]
    for v in stages.values():
        if "started" in v and "finished" in v:
            t = lambda x: _dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
            v["minutes"] = round((t(v["finished"]) - t(v["started"])).total_seconds() / 60, 1)
    return stages


def build_status(root: Path, key: str, now: _dt.datetime | None = None) -> dict:
    """Operational status for the page and status.json: when the site was built, the last analysis
    run record, work still to do, and the size of the stored data (the working tree; the git
    history of the data repository is measured separately by the workflow)."""
    from .changes import find_pending_reports
    from .ids import is_path_id
    from .pipeline import find_pending

    now = now or _dt.datetime.now(_dt.timezone.utc)
    _, catalog_gens = load_catalog(root)
    feeds = [fk for fk in sorted(catalog_gens) if all(is_path_id(x) for x in fk)]
    stored = {fk: list_analyses(root, *fk) for fk in feeds}
    runs = sorted((root / "runs").glob("*/*.json"))
    last_run = None
    if runs:
        doc = json.loads(runs[-1].read_text(encoding="utf-8"))
        last_run = {k: doc.get(k) for k in ("run_id", "started_at", "finished_at", "trigger")}
    base = root / "feeds"
    n_reports, report_bytes, report_max = _bytes(base.glob("*/*/changes/*/*.report.json.gz"))
    storage = {
        "reports": {"count": n_reports, "bytes": report_bytes, "max_bytes": report_max,
                    "avg_bytes": report_bytes // n_reports if n_reports else 0},
        "analyses_bytes": _bytes(p for p in base.glob("*/*/generations/*/*.json") if p.name != "content.json")[1],
        "signatures_bytes": _bytes(base.glob("*/*/generations/*/content.json"))[1],
        "diffs_bytes": _bytes(base.glob("*/*/diffs/*/*.json"))[1],
        "data_bytes": _bytes(p for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts)[1],
    }
    path = root / HISTORY_PATH
    history = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    storage["growth"] = growth(history, now.date())
    return {
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_run": last_run,
        "backlog": {"unanalysed": len(find_pending(catalog_gens, stored, key)),
                    "reports_pending": len(find_pending_reports(root, feeds, key, ENGINE_VERSION, catalog_gens))},
        "storage": storage,
    }


def compact_rules(feeds: list[dict]) -> dict[str, list[str]]:
    """Move each rule's severity and class out of the publications: returns {rule_id: [severity,
    class]} (the most common pair) and leaves in each publication only the count, or the full
    [count, severity, class] where that publication differs (some rules vary their severity)."""
    seen: dict[str, dict[tuple, int]] = {}
    for f in feeds:
        for g in f["generations"]:
            for rid, (_, sev, cls) in g["rules"].items():
                seen.setdefault(rid, {}).setdefault((sev, cls), 0)
                seen[rid][(sev, cls)] += 1
    meta = {rid: list(max(sorted(c), key=c.get)) for rid, c in sorted(seen.items())}
    for f in feeds:
        for g in f["generations"]:
            g["rules"] = {rid: v[0] if v[1:] == meta[rid] else v for rid, v in g["rules"].items()}
    return meta


def field_shares(fields: dict) -> dict:
    """{file: {"rows": n, field: percent of rows filled}} (data-model §15)."""
    out = {}
    for name, v in sorted(fields.items()):
        out[name] = dict({"rows": v["rows"]}, **{c: round(100 * n / v["rows"]) if v["rows"] else 0 for c, n in sorted(v["filled"].items())})
        if v.get("coordinates"):
            out[name]["coordinates"] = v["coordinates"]
    return out


def build_export(data_dir: Path, key: str, analyzer: Path | None = None) -> tuple[dict, dict[str, dict]]:
    """(export for the page, report files to write next to it)."""
    root = Path(data_dir)
    catalog_feeds, catalog_gens = load_catalog(root)
    feeds = []
    fields: dict[str, dict] = {}  # uid -> field shares, written as fields.json
    rule_ids: set[str] = set()
    for fk in sorted(catalog_gens):
        analyses = list_analyses(root, *fk)
        entries = catalog_gens[fk]
        ordered, _ = order_generations(GenerationRef(e["uid"], e["from_date"], e["published_at"]) for e in entries.values())
        gens = []
        last_digest = None
        for ref in ordered:
            if key not in analyses.get(ref.uid, {}):
                continue
            doc = json.loads(generation_path(root, *fk, ref.uid, key).read_text(encoding="utf-8"))
            gens.append(_summary(entries[ref.uid], doc))
            content = load_content(root, *fk, ref.uid)
            if content and content.get("fields"):
                fields[ref.uid] = field_shares(content["fields"])
            digest = content_digest(doc, content) if doc["validation_status"] != "FATAL" else None
            gens[-1]["equivalent_to_previous"] = digest is not None and digest == last_digest  # data-model §4.2
            last_digest = digest
            rule_ids.update(gens[-1]["rules"])
        if not gens:
            continue
        row = catalog_feeds.get(fk, {})
        feeds.append({
            "org_id": fk[0],
            "feed_id": fk[1],
            "name": row.get("feed_name", ""),
            "organization_name": row.get("organization_name", ""),
            "pref_id": row.get("feed_pref_id"),
            "license": (row.get("license") or {}).get("raw", ""),
            "is_discontinued": bool(row.get("is_discontinued")),
            "discontinued_date": row.get("discontinued_date"),
            "listed": row.get("listed", True),
            "generations": gens,  # oldest first (data-model §2)
        })
    report_index, files = report_files(root, feeds, ENGINE_VERSION)
    classify_pairs(root, feeds, report_index, ENGINE_VERSION)
    metrics = build_metrics(feeds, catalog_gens, report_index, key, ENGINE_VERSION,
                            _dt.datetime.now(_dt.timezone.utc).date())  # before compact_rules
    return {
        "schema": SCHEMA,
        "analysis_key": key,
        "feeds": feeds,
        "rule_titles": rule_titles(analyzer, rule_ids) if analyzer else {lang: {} for lang in LANGS},
        "rule_meta": compact_rules(feeds),  # publications keep counts only (see compact_rules)
        "engine_version": ENGINE_VERSION,
        "report_index": report_index,
        "status": build_status(root, key),
        "metrics": metrics,
        "fields": fields,
    }, files


# Storage budgets (data-model §10.2). The repository budget is the project's own limit, not
# GitHub's; the site limit is the GitHub Pages limit for a published site.
REPO_BUDGET_BYTES = 1 << 30
SITE_LIMIT_BYTES = 1 << 30
WARN_SHARE = 0.75


def storage_warnings(status: dict, repo_bytes: int | None) -> list[str]:
    """Warnings for sizes past WARN_SHARE of their budget."""
    out = []
    for name, size, budget in (("data repository (with history)", repo_bytes, REPO_BUDGET_BYTES),
                               ("web site", status.get("site_bytes"), SITE_LIMIT_BYTES)):
        if size is not None and size >= WARN_SHARE * budget:
            out.append(f"{name} is {size / 2**20:.0f} MiB, {size / budget:.0%} of its {budget / 2**20:.0f} MiB budget")
    return out
