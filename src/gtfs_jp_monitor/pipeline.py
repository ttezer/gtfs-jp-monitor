"""Incremental analysis run (data-model §8, §9).

catalog -> pending (uid, analysis_key) -> download -> analyze -> generation record
        -> feed.json -> consecutive diffs -> run record

A generation counts as done once a record exists for the analysis key, so an interrupted
run resumes where it stopped (data-model §8). Transient download failures write nothing and
are retried next run; analyzer-side failures are stored as FATAL records.
"""

from __future__ import annotations

import datetime as _dt
import json
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .analyzer import AnalyzerBinary, run_validate
from .canonical import write_json, write_json_if_changed
from .catalog import load_catalog
from .diff import sync_feed_diffs
from .download import DownloadError, download_zip
from .generation import (
    AnalyzerIdentity,
    AnalyzerOutputError,
    FeedMeta,
    GenerationMeta,
    build_fatal,
    build_generation,
)
from .ids import is_path_id
from .ordering import GenerationRef, order_generations
from .store import (
    analysis_key,
    build_feed_index,
    check_writable,
    generation_path,
    list_analyses,
    load_feed_index,
    write_feed_index,
)

RUN_SCHEMA = "gtfs-jp-monitor-run/1"

FeedKey = tuple[str, str]
Downloader = Callable[[str, Path], object]


@dataclass
class Pending:
    org_id: str
    feed_id: str
    depth: int  # 0 = newest generation of the feed
    entry: dict  # catalog generation entry


@dataclass
class RunReport:
    run_id: str
    counts: dict = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    run_file: str | None = None


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _iso(ts: _dt.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def find_pending(
    catalog_gens: dict[FeedKey, dict[str, dict]],
    stored: dict[FeedKey, dict[str, dict[str, str]]],
    key: str,
    only: set[FeedKey] | None = None,
) -> list[Pending]:
    """Generations present in the API, orderable, and without a record for `key`.

    Newest first, round-robin across feeds: every feed's newest generation comes before any
    feed's second newest, so a partial backfill still covers the current period everywhere.
    """
    pending: list[Pending] = []
    for fk in sorted(catalog_gens):
        if only is not None and fk not in only:
            continue
        entries = catalog_gens[fk]
        present = [e for e in entries.values() if e.get("present", True)]
        ordered, _unknown = order_generations(
            GenerationRef(e["uid"], e["from_date"], e["published_at"]) for e in present
        )
        done = stored.get(fk, {})
        for depth, ref in enumerate(reversed(ordered)):
            if key not in done.get(ref.uid, {}):
                pending.append(Pending(fk[0], fk[1], depth, entries[ref.uid]))
    pending.sort(key=lambda p: (p.depth, p.org_id, p.feed_id))
    return pending


def _previous_sha(root: Path, org_id: str, feed_id: str, uid: str, analyses: dict[str, str]) -> str | None:
    for key in sorted(analyses):
        path = generation_path(root, org_id, feed_id, uid, key)
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc["generation"]["sha256"]
    return None


def run_analysis(
    data_dir: Path,
    binary: AnalyzerBinary,
    profile: str,
    trigger: str = "local",
    limit: int | None = None,
    only: set[FeedKey] | None = None,
    timeout: float = 600,
    downloader: Callable[..., object] = download_zip,
    now: Callable[[], _dt.datetime] = _utc_now,
) -> RunReport:
    root = Path(data_dir)
    check_writable(root, binary.is_pinned)
    release_tag = binary.pinned_release or f"v{binary.version}"
    key = analysis_key(release_tag, profile)
    identity = AnalyzerIdentity(binary.version, release_tag, binary.sha256, binary.os, binary.arch, profile)

    started = now()
    report = RunReport(run_id=f"{started.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}")
    catalog_feeds, catalog_gens = load_catalog(root)
    stored = {fk: list_analyses(root, *fk) for fk in catalog_gens}
    pending = find_pending(catalog_gens, stored, key, only)
    selected = pending if limit is None else pending[:limit]
    counts = {"feeds_scanned": len(catalog_gens), "generations_seen": sum(len(v) for v in catalog_gens.values()),
              "new_generations": len(pending), "analyzed": 0, "skipped": len(pending) - len(selected), "failed": 0}

    source_changed: dict[FeedKey, set[str]] = {}
    with tempfile.TemporaryDirectory(prefix="gtfs-jp-monitor-") as tmp:
        work = Path(tmp)
        for item in selected:
            fk = (item.org_id, item.feed_id)
            entry = item.entry
            uid = entry["uid"]
            feed_row = catalog_feeds.get(fk, {})
            record = {"org_id": item.org_id, "feed_id": item.feed_id, "uid": uid,
                      "rid_observed": entry["rid_observed"], "action": "failed", "validation_status": None,
                      "duration_ms": 0, "error_code": None, "message": None}
            zip_path = work / f"{uid}.zip"
            try:
                try:
                    downloaded = downloader(entry["gtfs_url"], zip_path)
                except DownloadError as err:
                    record.update(error_code=err.code, message=err.detail[:2000])
                    counts["failed"] += 1
                    if err.code == "SOURCE_UNAVAILABLE":
                        report.warnings.append({"code": "SOURCE_UNAVAILABLE", "org_id": item.org_id,
                                                "feed_id": item.feed_id, "uid": uid, "detail": err.detail})
                    report.items.append(record)
                    continue

                prev_sha = _previous_sha(root, item.org_id, item.feed_id, uid, stored.get(fk, {}).get(uid, {}))
                if prev_sha is not None and prev_sha != downloaded.sha256:
                    source_changed.setdefault(fk, set()).add(uid)
                    report.warnings.append({"code": "SOURCE_CHANGED", "org_id": item.org_id, "feed_id": item.feed_id,
                                            "uid": uid, "detail": "ZIP checksum differs from the stored analysis"})

                # Missing source values stay null; nothing is guessed (data-model §5).
                meta = GenerationMeta(uid, downloaded.sha256, entry["published_at"], entry["from_date"],
                                      entry["to_date"], feed_row.get("license", {}).get("raw", ""))
                feed_meta = FeedMeta(item.org_id, item.feed_id, feed_row.get("feed_name", ""))
                run = run_validate(binary, zip_path, entry["from_date"], profile, work, timeout=timeout)
                record["duration_ms"] = run.duration_ms
                if run.report is None:
                    doc = build_fatal(feed_meta, meta, identity, run.error_code or "ANALYZER_ERROR")
                    record["message"] = run.stderr_tail[-2000:] or None
                else:
                    try:
                        doc = build_generation(run.report, feed_meta, meta, identity)
                    except AnalyzerOutputError as err:
                        doc = build_fatal(feed_meta, meta, identity, "INVALID_REPORT")
                        record["message"] = str(err)[:2000]
                path = generation_path(root, item.org_id, item.feed_id, uid, key)
                write_json(path, doc)
                report.changed_files.append(str(path.relative_to(root)))
                stored.setdefault(fk, {}).setdefault(uid, {})[key] = doc["validation_status"]
                record.update(action="analyzed", validation_status=doc["validation_status"],
                              error_code=doc["fatal"]["code"] if doc["fatal"] else None)
                counts["analyzed"] += 1
                report.items.append(record)
            finally:
                zip_path.unlink(missing_ok=True)

    for fk in sorted(catalog_gens):
        if fk not in catalog_feeds or not all(is_path_id(x) for x in fk):
            continue
        ordered_entries = _catalog_order(catalog_gens[fk])
        doc = build_feed_index(catalog_feeds[fk], ordered_entries, stored.get(fk, {}), key,
                               previous=load_feed_index(root, *fk), source_changed=source_changed.get(fk, set()))
        if write_feed_index(root, doc):
            report.changed_files.append(f"feeds/{fk[0]}/{fk[1]}/feed.json")
        diffs = sync_feed_diffs(root, doc, key)
        report.changed_files.extend(diffs.written + diffs.deleted)
        for old_uid, new_uid, reason in diffs.refused:
            report.warnings.append({"code": "DIFF_REFUSED", "org_id": fk[0], "feed_id": fk[1], "uid": new_uid,
                                    "detail": f"{old_uid} -> {new_uid}: {reason}"[:2000]})

    report.counts = counts
    if report.changed_files or report.warnings or counts["failed"]:
        finished = now()
        run_doc = {
            "schema": RUN_SCHEMA, "run_id": report.run_id, "started_at": _iso(started), "finished_at": _iso(finished),
            "trigger": trigger,
            "analyzer": {"release_tag": release_tag, "binary_sha256": binary.sha256,
                         "platform": {"os": binary.os, "arch": binary.arch}},
            "counts": counts, "items": report.items, "warnings": report.warnings,
        }
        run_path = root / "runs" / started.strftime("%Y") / f"{report.run_id}.json"
        write_json_if_changed(run_path, run_doc)
        report.run_file = str(run_path.relative_to(root))
    return report


def _catalog_order(by_uid: dict[str, dict]) -> list[dict]:
    ordered, unknown = order_generations(
        GenerationRef(e["uid"], e["from_date"], e["published_at"]) for e in by_uid.values()
    )
    return [by_uid[r.uid] for r in ordered + unknown]

