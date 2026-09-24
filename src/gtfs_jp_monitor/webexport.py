"""Compact export of the data repository for the web page (prototype of the public export).

Language-neutral data plus per-language rule titles taken from the analyzer's own catalog.
Validation diffs are not exported: the page computes them from generation summaries with the
same rules as `diff.build_diff` (data-model §9).
"""

from __future__ import annotations

import gzip
import json
import subprocess
from pathlib import Path

from .catalog import load_catalog
from .ordering import GenerationRef, order_generations
from .store import content_digest, generation_path, list_analyses

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


def load_reports(reports_dir: Path, feeds: set[tuple[str, str]]) -> dict[str, dict]:
    """Semantic reports keyed "<old_uid>__<new_uid>", for exported feeds only."""
    reports: dict[str, dict] = {}
    for path in sorted(Path(reports_dir).iterdir()):
        if path.name.endswith(".report.json.gz"):
            doc = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        elif path.name.endswith(".report.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
        else:
            continue
        h = doc["header"]
        if (h["feed"]["org_id"], h["feed"]["feed_id"]) in feeds:
            reports[f"{h['old']['uid']}__{h['new']['uid']}"] = doc
    return reports


def build_export(data_dir: Path, key: str, analyzer: Path | None = None, reports_dir: Path | None = None) -> dict:
    root = Path(data_dir)
    catalog_feeds, catalog_gens = load_catalog(root)
    feeds = []
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
            digest = content_digest(doc) if doc["validation_status"] != "FATAL" else None
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
    return {
        "schema": SCHEMA,
        "analysis_key": key,
        "feeds": feeds,
        "rule_titles": rule_titles(analyzer, rule_ids) if analyzer else {lang: {} for lang in LANGS},
        "reports": load_reports(reports_dir, {(f["org_id"], f["feed_id"]) for f in feeds}) if reports_dir else {},
    }
