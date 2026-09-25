"""Data repository layout and per-feed index (data-model §4, feed.schema.json).

feeds/<org_id>/<feed_id>/
    feed.json
    generations/<uid>/<analysis_key>.json      analysis_key = <release_tag>__<gtfs_jp_profile>
    generations/<uid>/content.json             content signature of the ZIP (signature.py)
    diffs/<analysis_key>/<old_uid>__<new_uid>.json
    changes/<engine_version>/<old_uid>__<new_uid>.report.json.gz   semantic change report
    changes/<engine_version>/<old_uid>__<new_uid>.error.json       report could not be built
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .analyzer import PROFILES
import hashlib

from .canonical import dumps, write_json_if_changed
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


def content_path(root: Path, org_id: str, feed_id: str, uid: str) -> Path:
    return feed_dir(root, org_id, feed_id) / "generations" / require_uid(uid) / "content.json"


def load_content(root: Path, org_id: str, feed_id: str, uid: str) -> dict | None:
    path = content_path(root, org_id, feed_id, uid)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def diff_path(root: Path, org_id: str, feed_id: str, key: str, old_uid: str, new_uid: str) -> Path:
    split_key(key)
    return feed_dir(root, org_id, feed_id) / "diffs" / key / f"{require_uid(old_uid)}__{require_uid(new_uid)}.json"


_ENGINE_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def change_path(root: Path, org_id: str, feed_id: str, engine_version: str, old_uid: str, new_uid: str,
                suffix: str = ".report.json.gz") -> Path:
    if not _ENGINE_RE.fullmatch(engine_version) or suffix not in (".report.json.gz", ".error.json"):
        raise StoreError(f"invalid change path parts {engine_version!r} {suffix!r}")
    return (feed_dir(root, org_id, feed_id) / "changes" / engine_version
            / f"{require_uid(old_uid)}__{require_uid(new_uid)}{suffix}")


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


# Two publications are equivalent (data-model §4.2) when their analysis summaries and the content
# signatures of their ZIPs are equal. The ZIP bytes may differ; the summary alone is not enough,
# since values can change while every count and rule result stays the same.
CONTENT_FIELDS = ("validation_status", "partial", "publishable", "coverage_complete", "is_gtfs_jp",
                  "scores", "metrics", "file_row_counts", "rules")


def summary_digest(doc: dict) -> str:
    return hashlib.sha256(dumps({k: doc[k] for k in CONTENT_FIELDS}).encode("utf-8")).hexdigest()


def content_digest(doc: dict, content: dict | None) -> str | None:
    """None (never equivalent) without a signature for the analysed ZIP."""
    if content is None or content.get("zip_sha256") != doc["generation"]["sha256"]:
        return None
    return hashlib.sha256(f"{summary_digest(doc)}:{content['signature']}".encode("utf-8")).hexdigest()


def analysis_digests(root: Path, org_id: str, feed_id: str) -> dict[str, dict[str, str]]:
    """{uid: {analysis_key: content digest}} for stored analyses that are not FATAL and have a
    content signature."""
    base = feed_dir(root, org_id, feed_id) / "generations"
    found: dict[str, dict[str, str]] = {}
    if not base.is_dir():
        return found
    for uid_dir in sorted(base.iterdir()):
        if not (uid_dir.is_dir() and is_uid(uid_dir.name)):
            continue
        content = load_content(root, org_id, feed_id, uid_dir.name)
        for file in sorted(uid_dir.glob("*.json")):
            if not _KEY_RE.fullmatch(file.stem):
                continue
            doc = json.loads(file.read_text(encoding="utf-8"))
            if doc["validation_status"] != "FATAL" and (digest := content_digest(doc, content)) is not None:
                found.setdefault(uid_dir.name, {})[file.stem] = digest
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
    digests: dict[str, dict[str, str]] | None = None,
) -> dict:
    """feed.json for one feed. `catalog_generations` must already be in data-model §2 order.

    The canonical analysis of a uid is `canonical_key` when stored; otherwise the previous
    canonical is kept (so a partial re-analysis never drops a pointer).
    """
    prev_by_uid = {g["uid"]: g for g in (previous or {}).get("generations", [])}
    digests = digests or {}
    last_digest: dict[str, str | None] = {}  # per analysis key, the previous publication's digest
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
                {"release_tag": split_key(k)[0], "gtfs_jp_profile": split_key(k)[1], "validation_status": v,
                 "equivalent_to_previous": _equivalent(uid, k, status, digests, last_digest)}
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


def _equivalent(uid: str, key: str, source_status: str, digests: dict, last_digest: dict) -> bool:
    if source_status == "ORDERING_UNKNOWN":
        return False
    digest = digests.get(uid, {}).get(key)
    same = digest is not None and last_digest.get(key) == digest
    last_digest[key] = digest
    return same


def write_feed_index(root: Path, doc: dict) -> bool:
    return write_json_if_changed(feed_json_path(root, doc["org_id"], doc["feed_id"]), doc)


def _is_iso(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None
