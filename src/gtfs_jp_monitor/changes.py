"""Semantic change reports for consecutive publications (docs/semantic/03-report.md, Storage).

Pairs are neighbouring analysed publications as the web page shows them (see report_pairs);
equivalent pairs get no report. Newest pairs first, round-robin across feeds, so a partial
backfill covers the current change of every feed first.

A report that cannot be built leaves an .error.json marker for this engine version, so a
broken pair is not retried every day; a new engine version retries every pair.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from gtfs_jp_semantic import ENGINE_VERSION
from gtfs_jp_semantic.rawdiff import gzip_bytes
from gtfs_jp_semantic.report import build_report

from .canonical import write_json
from .catalog import load_catalog
from .download import DownloadError, download_zip
from .ids import is_path_id
from .store import change_path, diff_path, generation_path, load_feed_index, split_key

ERROR_SCHEMA = "gtfs-jp-monitor-change-error/1"
FeedKey = tuple[str, str]


@dataclass(frozen=True)
class PendingReport:
    org_id: str
    feed_id: str
    depth: int  # 0 = newest pair of the feed
    old_uid: str
    new_uid: str


@dataclass
class ReportRun:
    counts: Counter = field(default_factory=Counter)
    items: list[dict] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)


def report_pairs(feed_index: dict, key: str) -> list[tuple[str, str]]:
    """Neighbouring publications as the page shows them: analysed and not FATAL for `key`.

    Unanalysed publications are not on the page and are passed over; a FATAL one breaks the
    chain. Equivalent pairs get no report. PARTIAL analyses count: the semantic engine reads
    the ZIP, not the validation result.
    """
    pairs: list[tuple[str, str]] = []
    previous: str | None = None
    for entry in feed_index["generations"]:
        if entry["source_status"] == "ORDERING_UNKNOWN":
            continue
        analysis = next((a for a in entry["analyses"] if f"{a['release_tag']}__{a['gtfs_jp_profile']}" == key), None)
        if analysis is None:
            continue
        if analysis["validation_status"] == "FATAL":
            previous = None
            continue
        if previous is not None and not analysis.get("equivalent_to_previous"):
            pairs.append((previous, entry["uid"]))
        previous = entry["uid"]
    return pairs


def find_pending_reports(root: Path, feeds: list[FeedKey], key: str, engine_version: str = ENGINE_VERSION) -> list[PendingReport]:
    pending = []
    for fk in feeds:
        index = load_feed_index(root, *fk)
        if index is None:
            continue
        pairs = report_pairs(index, key)
        for depth, (old_uid, new_uid) in enumerate(reversed(pairs)):
            done = any(change_path(root, *fk, engine_version, old_uid, new_uid, suffix).exists()
                       for suffix in (".report.json.gz", ".error.json"))
            if not done:
                pending.append(PendingReport(fk[0], fk[1], depth, old_uid, new_uid))
    pending.sort(key=lambda p: (p.depth, p.org_id, p.feed_id))
    return pending


def _publication(entry: dict) -> dict:
    return {k: entry.get(k) for k in ("uid", "from_date", "to_date", "published_at", "memo")}


def run_reports(data_dir: Path, key: str, limit: int | None = None, only: set[FeedKey] | None = None,
                downloader: Callable[..., object] = download_zip, engine_version: str = ENGINE_VERSION) -> ReportRun:
    root = Path(data_dir)
    split_key(key)
    _, catalog_gens = load_catalog(root)
    feeds = [fk for fk in sorted(catalog_gens) if all(is_path_id(x) for x in fk) and (only is None or fk in only)]
    pending = find_pending_reports(root, feeds, key, engine_version)
    selected = pending if limit is None else pending[:limit]
    run = ReportRun()
    run.counts.update(pending=len(pending), selected=len(selected))
    # Each ZIP is downloaded once per run and removed after its last use.
    uses = Counter(uid for p in selected for uid in (p.old_uid, p.new_uid))
    with tempfile.TemporaryDirectory(prefix="gtfs-jp-changes-") as tmp:
        zips: dict[str, Path] = {}
        for p in selected:
            fk = (p.org_id, p.feed_id)
            record = {"org_id": p.org_id, "feed_id": p.feed_id, "old_uid": p.old_uid, "new_uid": p.new_uid,
                      "action": None, "code": None}
            try:
                error = None
                for uid in (p.old_uid, p.new_uid):
                    if uid in zips:
                        continue
                    entry = catalog_gens[fk][uid]
                    target = Path(tmp) / f"{uid}.zip"
                    try:
                        downloaded = downloader(entry["gtfs_url"], target)
                    except DownloadError as err:
                        error = (err.code, err.detail)
                        break
                    analysed = json.loads(generation_path(root, *fk, uid, key).read_text(encoding="utf-8"))
                    if analysed["generation"]["sha256"] != downloaded.sha256:
                        target.unlink(missing_ok=True)
                        error = ("SOURCE_CHANGED", f"{uid}: ZIP differs from the analysed one")
                        break
                    zips[uid] = target
                if error is not None:
                    code, detail = error
                    if code in ("SOURCE_UNAVAILABLE", "SOURCE_CHANGED"):  # lasting: mark; others retry next run
                        _mark(root, p, engine_version, code, detail, run)
                    record.update(action="failed", code=code)
                    run.counts["failed"] += 1
                    continue
                entries = catalog_gens[fk]
                quality = None
                path = diff_path(root, *fk, key, p.old_uid, p.new_uid)
                if path.is_file():
                    scores = json.loads(path.read_text(encoding="utf-8"))["scores"]
                    if scores:
                        quality = {"publish": scores["publish"], "overall": scores["overall"]}
                try:
                    report, _ = build_report(zips[p.old_uid], zips[p.new_uid], feed={"org_id": p.org_id, "feed_id": p.feed_id},
                                             old_pub=_publication(entries[p.old_uid]), new_pub=_publication(entries[p.new_uid]),
                                             summary_quality=quality, analysis_key=key if path.is_file() else None)
                except Exception as err:  # any engine failure is recorded, never fatal to the run
                    _mark(root, p, engine_version, "ENGINE_ERROR", f"{type(err).__name__}: {err}", run)
                    record.update(action="failed", code="ENGINE_ERROR")
                    run.counts["failed"] += 1
                    continue
                out = change_path(root, *fk, engine_version, p.old_uid, p.new_uid)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(gzip_bytes(report))
                run.changed_files.append(str(out.relative_to(root)))
                record["action"] = "reported"
                run.counts["reported"] += 1
            finally:
                run.items.append(record)
                for uid in (p.old_uid, p.new_uid):
                    uses[uid] -= 1
                    if uses[uid] == 0 and uid in zips:
                        zips.pop(uid).unlink(missing_ok=True)
    return run


def _mark(root: Path, p: PendingReport, engine_version: str, code: str, detail: str, run: ReportRun) -> None:
    path = change_path(root, p.org_id, p.feed_id, engine_version, p.old_uid, p.new_uid, ".error.json")
    write_json(path, {"schema": ERROR_SCHEMA, "engine_version": engine_version, "code": code, "detail": detail[:2000]})
    run.changed_files.append(str(path.relative_to(root)))
