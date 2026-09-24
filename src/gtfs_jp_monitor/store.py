"""Data repository layout and per-feed index (data-model §4, feed.schema.json).

feeds/<org_id>/<feed_id>/
    feed.json
    generations/<uid>/<analysis_key>.json      analysis_key = <release_tag>__<gtfs_jp_profile>
    diffs/<analysis_key>/<old_uid>__<new_uid>.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .analyzer import PROFILES
from .canonical import write_json_if_changed
from .ids import feed_dir, is_uid, require_uid

FEED_SCHEMA = "gtfs-jp-monitor-feed/1"
UNPINNED_MARKER = ".unpinned-scratch"
_KEY_RE = re.compile(r"(v[0-9]+\.[0-9]+\.[0-9]+)__(auto|v3|v4)")


class StoreError(RuntimeError):
    pass


def analysis_key(release_tag: str, profile: str) -> str:
    key = f"{release_tag}__{profile}"
    if not _KEY_RE.fullmatch(key) or profile not in PROFILES:
        raise StoreError(f"invalid analysis key {key!r}")
    return key


def split_key(key: str) -> tuple[str, str]:
    match = _KEY_RE.fullmatch(key)
    if not match:
        raise StoreError(f"invalid analysis key {key!r}")
    return match.group(1), match.group(2)


def generation_path(root: Path, org_id: str, feed_id: str, uid: str, key: str) -> Path:
    split_key(key)
    return feed_dir(root, org_id, feed_id) / "generations" / require_uid(uid) / f"{key}.json"


def diff_path(root: Path, org_id: str, feed_id: str, key: str, old_uid: str, new_uid: str) -> Path:
    split_key(key)
    return feed_dir(root, org_id, feed_id) / "diffs" / key / f"{require_uid(old_uid)}__{require_uid(new_uid)}.json"


def feed_json_path(root: Path, org_id: str, feed_id: str) -> Path:
    return feed_dir(root, org_id, feed_id) / "feed.json"


def check_writable(root: Path, pinned: bool) -> None:
    """Unpinned analyzer binaries may only write into an explicitly marked scratch directory."""
    if not pinned and not (Path(root) / UNPINNED_MARKER).is_file():
        raise StoreError(
            f"refusing to write results of an unpinned analyzer binary into {root}; "
            f"create {UNPINNED_MARKER} there only if it is a scratch directory"
        )


def list_analyses(root: Path, org_id: str, feed_id: str) -> dict[str, dict[str, str]]:
    """{uid: {analysis_key: validation_status}} for every stored analysis file of a feed."""
    base = feed_dir(root, org_id, feed_id) / "generations"
    found: dict[str, dict[str, str]] = {}
    if not base.is_dir():
        return found
    for uid_dir in sorted(base.iterdir()):
        if not (uid_dir.is_dir() and is_uid(uid_dir.name)):
            continue
        for file in sorted(uid_dir.glob("*.json")):
            key = file.stem
            if not _KEY_RE.fullmatch(key):
                continue
            doc = json.loads(file.read_text(encoding="utf-8"))
            found.setdefault(uid_dir.name, {})[key] = doc["validation_status"]
    return found


def load_generation(root: Path, org_id: str, feed_id: str, uid: str, key: str) -> dict | None:
    path = generation_path(root, org_id, feed_id, uid, key)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def load_feed_index(root: Path, org_id: str, feed_id: str) -> dict | None:
    path = feed_json_path(root, org_id, feed_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def build_feed_index(
    catalog_feed: dict,
    catalog_generations: list[dict],
    analyses: dict[str, dict[str, str]],
    canonical_key: str,
    previous: dict | None = None,
    source_changed: set[str] = frozenset(),
) -> dict:
    """feed.json for one feed. `catalog_generations` must already be in data-model §2 order.

    The canonical analysis of a uid is `canonical_key` when stored; otherwise the previous
    canonical is kept (so a partial re-analysis never drops a pointer).
    """
    prev_by_uid = {g["uid"]: g for g in (previous or {}).get("generations", [])}
    entries = []
    for gen in catalog_generations:
        uid = gen["uid"]
        stored = analyses.get(uid, {})
        old = prev_by_uid.get(uid, {})
        if not gen.get("present", True):
            status = "SOURCE_UNAVAILABLE"
        elif uid in source_changed or old.get("source_status") == "SOURCE_CHANGED":
            status = "SOURCE_CHANGED"
        elif not gen.get("from_date"):
            status = "ORDERING_UNKNOWN"
        else:
            status = "AVAILABLE"
        if canonical_key in stored:
            release, profile = split_key(canonical_key)
            canonical = {"release_tag": release, "gtfs_jp_profile": profile}
        else:
            canonical = old.get("canonical")
        entries.append({
            "uid": uid,
            "from_date": gen.get("from_date"),
            "to_date": gen.get("to_date"),
            "published_at": gen.get("published_at"),
            "source_status": status,
            "analyses": [
                {"release_tag": split_key(k)[0], "gtfs_jp_profile": split_key(k)[1], "validation_status": v}
                for k, v in sorted(stored.items())
            ],
            "canonical": canonical,
        })
    return {
        "schema": FEED_SCHEMA,
        "org_id": catalog_feed["org_id"],
        "feed_id": catalog_feed["feed_id"],
        "name": catalog_feed["feed_name"],
        "license": catalog_feed["license"],
        "discontinued": {
            "is_discontinued": catalog_feed["is_discontinued"],
            "date": catalog_feed["discontinued_date"] if _is_iso(catalog_feed["discontinued_date"]) else None,
        },
        "generations": entries,
    }


def write_feed_index(root: Path, doc: dict) -> bool:
    return write_json_if_changed(feed_json_path(root, doc["org_id"], doc["feed_id"]), doc)


def _is_iso(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None
