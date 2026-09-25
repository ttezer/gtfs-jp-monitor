"""Semantic change reports for consecutive publications (docs/semantic/03-report.md, Storage).

Pairs are neighbouring analysed publications as the web page shows them (see report_pairs);
equivalent pairs get no report. Newest pairs first, round-robin across feeds, so a partial
backfill covers the current change of every feed first.

A report that cannot be built leaves an .error.json marker for this engine version, so a
broken pair is not retried every day; a new engine version retries every pair. An engine
error is also retried once the engine code changes (engine_build), so a fix reaches the
failed pairs without rebuilding every report.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import gtfs_jp_semantic
from gtfs_jp_semantic import ENGINE_VERSION
from gtfs_jp_semantic.rawdiff import gzip_bytes
from gtfs_jp_semantic.report import build_report

from .canonical import write_json
from .catalog import load_catalog
from .download import DownloadError, download_zip
from .ids import is_path_id
from .store import change_path, diff_path, generation_path, load_feed_index, split_key

ERROR_SCHEMA = "gtfs-jp-monitor-change-error/1"


def engine_build() -> str:
    """Digest of the semantic engine's code and data files, in a fixed order."""
    base = Path(gtfs_jp_semantic.__file__).parent
    h = hashlib.sha256()
    for path in sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in (".py", ".json")):
        h.update(str(path.relative_to(base)).encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:16]
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


PAGE_BACK = 3  # the page shows the current publication and three before it


def page_pairs(feed_index: dict, key: str, entries: dict[str, dict]) -> list[tuple[str, str]]:
    """Non-neighbouring pairs among the publications the page shows, so any two of them can be
    compared: publications grouped as the page groups them (analysed; an equivalent one joins
    the previous group), from PAGE_BACK groups before the current one to the newest. A pair is
    (last uid of the older group, first uid of the newer one); FATAL groups take no part."""
    groups: list[list] = []  # [first uid, last uid, fatal]
    for entry in feed_index["generations"]:
        if entry["source_status"] == "ORDERING_UNKNOWN":
            continue
        analysis = next((a for a in entry["analyses"] if f"{a['release_tag']}__{a['gtfs_jp_profile']}" == key), None)
        if analysis is None:
            continue
        fatal = analysis["validation_status"] == "FATAL"
        if groups and not fatal and not groups[-1][2] and analysis.get("equivalent_to_previous"):
            groups[-1][1] = entry["uid"]
            continue
        groups.append([entry["uid"], entry["uid"], fatal])
    rid = lambda uid: (entries.get(uid) or {}).get("rid_observed") or ""
    current = next((i for i in range(len(groups) - 1, -1, -1) if rid(groups[i][0]) == "current" or rid(groups[i][1]) == "current"),
                   next((i for i in range(len(groups) - 1, -1, -1) if not rid(groups[i][1]).startswith("next_")), len(groups) - 1))
    window = [g for g in groups[max(0, current - PAGE_BACK):] if not g[2]]
    idx = {id(g): i for i, g in enumerate(groups)}
    return [(a[1], b[0]) for i, a in enumerate(window) for b in window[i + 1:] if idx[id(b)] - idx[id(a)] > 1]


def find_pending_reports(root: Path, feeds: list[FeedKey], key: str, engine_version: str = ENGINE_VERSION,
                         catalog_gens: dict | None = None) -> list[PendingReport]:
    build = engine_build()
    pending = []
    for fk in feeds:
        index = load_feed_index(root, *fk)
        if index is None:
            continue
        pairs = list(reversed(report_pairs(index, key)))
        # Pairs the page can compare beyond neighbours come after the two newest neighbour rounds.
        extra = page_pairs(index, key, (catalog_gens or {}).get(fk, {}))
        ranked = [(depth, pair) for depth, pair in enumerate(pairs)] + [(2, pair) for pair in extra]
        for depth, (old_uid, new_uid) in ranked:
            if change_path(root, *fk, engine_version, old_uid, new_uid).exists():
                continue
            marker = change_path(root, *fk, engine_version, old_uid, new_uid, ".error.json")
            if marker.exists():
                doc = json.loads(marker.read_text(encoding="utf-8"))
                if not (doc.get("code") == "ENGINE_ERROR" and doc.get("engine_build") != build):
                    continue  # lasting failure, or an engine error this code already produced
            pending.append(PendingReport(fk[0], fk[1], depth, old_uid, new_uid))
    pending.sort(key=lambda p: (p.depth, p.org_id, p.feed_id))
    return pending


def _publication(entry: dict) -> dict:
    return {k: entry.get(k) for k in ("uid", "from_date", "to_date", "published_at", "memo")}


def run_reports(data_dir: Path, key: str, limit: int | None = None, only: set[FeedKey] | None = None,
                downloader: Callable[..., object] = download_zip, engine_version: str = ENGINE_VERSION,
                max_seconds: float | None = None, clock: Callable[[], float] = time.monotonic) -> ReportRun:
    """Build pending reports. max_seconds is a time budget: no new pair starts after it, so the
    workflow never reaches its own time limit and loses the run; the rest waits for the next run."""
    root = Path(data_dir)
    deadline = None if max_seconds is None else clock() + max_seconds
    split_key(key)
    _, catalog_gens = load_catalog(root)
    feeds = [fk for fk in sorted(catalog_gens) if all(is_path_id(x) for x in fk) and (only is None or fk in only)]
    pending = find_pending_reports(root, feeds, key, engine_version, catalog_gens)
    selected = pending if limit is None else pending[:limit]
    run = ReportRun()
    run.counts.update(pending=len(pending), selected=len(selected))
    # Each ZIP is downloaded once per run and removed after its last use.
    uses = Counter(uid for p in selected for uid in (p.old_uid, p.new_uid))
    with tempfile.TemporaryDirectory(prefix="gtfs-jp-changes-") as tmp:
        zips: dict[str, Path] = {}
        for p in selected:
            if deadline is not None and clock() >= deadline:
                run.counts["deferred"] += 1
                continue
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
    write_json(path, {"schema": ERROR_SCHEMA, "engine_version": engine_version, "engine_build": engine_build(),
                      "code": code, "detail": detail[:2000]})
    run.changed_files.append(str(path.relative_to(root)))
