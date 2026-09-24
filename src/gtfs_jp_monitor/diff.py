"""Validation diff v1 (data-model §9).

`build_diff` is the reference algorithm: the same rules must be reproduced by any client that
compares two arbitrary generations. `sync_feed_diffs` keeps diffs/<analysis_key>/ equal to the
set of consecutive COMPLETE pairs, deleting pairs that became stale (a generation inserted in
the middle turns A__C into A__B and B__C).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .canonical import delta1, write_json_if_changed
from .ids import feed_dir, is_uid
from .store import diff_path, generation_path, split_key

SCHEMA = "gtfs-jp-monitor-diff/1"
_SCORES = ("publish", "overall", "spec", "interop", "quality", "analytics")
_COUNT_METRICS = ("routes", "stops", "trips", "shapes", "active_service_days")


class DiffError(ValueError):
    pass


def _identity(doc: dict) -> dict:
    a = doc["analyzer"]
    return {
        "version": a["version"],
        "release_tag": a["release_tag"],
        "binary_sha256": a["binary_sha256"],
        "platform": a["platform"],
        "gtfs_jp_profile": a["gtfs_jp_profile"],
    }


def _rule_buckets(before: dict, after: dict) -> dict:
    buckets: dict[str, dict] = {"fixed": {}, "new": {}, "increased": {}, "decreased": {}, "same": {}}
    for rule_id in sorted(set(before) | set(after)):
        b = before.get(rule_id, {}).get("count", 0)
        a = after.get(rule_id, {}).get("count", 0)
        if b > 0 and a == 0:
            bucket = "fixed"
        elif b == 0 and a > 0:
            bucket = "new"
        elif a > b:
            bucket = "increased"
        elif a < b:
            bucket = "decreased"
        else:
            bucket = "same"
        buckets[bucket][rule_id] = {"before": b, "after": a}
    return buckets


def build_diff(old: dict, new: dict, skipped_uids: list[str] = ()) -> dict:
    for side, doc in (("old", old), ("new", new)):
        if doc.get("validation_status") != "COMPLETE":
            raise DiffError(f"{side} generation is {doc.get('validation_status')!r}, not COMPLETE")
    if (old["feed"]["org_id"], old["feed"]["feed_id"]) != (new["feed"]["org_id"], new["feed"]["feed_id"]):
        raise DiffError("generations belong to different feeds")
    if old["generation"]["uid"] == new["generation"]["uid"]:
        raise DiffError("old and new are the same generation")
    if _identity(old) != _identity(new):
        raise DiffError("generations were analysed with different analyzer identities")

    om, nm = old["metrics"], new["metrics"]
    files = {}
    for name in sorted(set(old["file_row_counts"]) | set(new["file_row_counts"])):
        b, a = old["file_row_counts"].get(name), new["file_row_counts"].get(name)
        files[name] = {"before": b, "after": a, "delta": (a or 0) - (b or 0)}

    metrics = {m: {"before": om[m], "after": nm[m], "delta": nm[m] - om[m]} for m in _COUNT_METRICS}
    metrics["avg_daily_trips"] = {
        "before": om["avg_daily_trips"],
        "after": nm["avg_daily_trips"],
        "delta": delta1(om["avg_daily_trips"], nm["avg_daily_trips"]),
    }
    return {
        "schema": SCHEMA,
        "feed": {"org_id": old["feed"]["org_id"], "feed_id": old["feed"]["feed_id"]},
        "old_uid": old["generation"]["uid"],
        "new_uid": new["generation"]["uid"],
        "analyzer": _identity(old),
        "skipped_uids": list(skipped_uids),
        "publishable": {"before": old["publishable"], "after": new["publishable"]},
        "scores": {
            s: {"before": old["scores"][s], "after": new["scores"][s], "delta": delta1(old["scores"][s], new["scores"][s])}
            for s in _SCORES
        },
        "metrics": metrics,
        "files": files,
        "ranges": {
            r: {"before": om[r], "after": nm[r]} for r in ("service_range", "feed_validity")
        },
        "rules": _rule_buckets(old["rules"], new["rules"]),
    }


@dataclass(frozen=True)
class Pair:
    old_uid: str
    new_uid: str
    skipped_uids: tuple[str, ...]


def consecutive_pairs(feed_index: dict, key: str) -> list[Pair]:
    """Neighbouring COMPLETE generations (for `key`) in feed.json order; others in between are skipped."""
    pairs: list[Pair] = []
    previous: str | None = None
    skipped: list[str] = []
    for entry in feed_index["generations"]:
        if entry["source_status"] == "ORDERING_UNKNOWN":
            continue
        status = {f"{a['release_tag']}__{a['gtfs_jp_profile']}": a["validation_status"] for a in entry["analyses"]}.get(key)
        if status != "COMPLETE":
            if previous is not None:
                skipped.append(entry["uid"])
            continue
        if previous is not None:
            pairs.append(Pair(previous, entry["uid"], tuple(skipped)))
        previous, skipped = entry["uid"], []
    return pairs


@dataclass
class DiffSync:
    written: list[str]
    deleted: list[str]
    refused: list[tuple[str, str, str]]  # (old_uid, new_uid, reason)


def sync_feed_diffs(root: Path, feed_index: dict, key: str) -> DiffSync:
    split_key(key)
    org_id, feed_id = feed_index["org_id"], feed_index["feed_id"]
    result = DiffSync([], [], [])
    wanted: set[Path] = set()
    for pair in consecutive_pairs(feed_index, key):
        old = json.loads(generation_path(root, org_id, feed_id, pair.old_uid, key).read_text(encoding="utf-8"))
        new = json.loads(generation_path(root, org_id, feed_id, pair.new_uid, key).read_text(encoding="utf-8"))
        path = diff_path(root, org_id, feed_id, key, pair.old_uid, pair.new_uid)
        try:
            doc = build_diff(old, new, list(pair.skipped_uids))
        except DiffError as err:
            result.refused.append((pair.old_uid, pair.new_uid, str(err)))
            continue
        wanted.add(path)
        if write_json_if_changed(path, doc):
            result.written.append(str(path.relative_to(root)))

    directory = feed_dir(root, org_id, feed_id) / "diffs" / key
    if directory.is_dir():
        for file in sorted(directory.glob("*.json")):
            left, sep, right = file.stem.partition("__")
            # Only files that look like our own pair files are ever removed.
            if sep and is_uid(left) and is_uid(right) and file not in wanted:
                file.unlink()
                result.deleted.append(str(file.relative_to(root)))
    return result
