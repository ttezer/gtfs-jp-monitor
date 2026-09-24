"""Catalog sync: gtfs-data.jp -> catalog/feeds.json and catalog/generations/<org>/<feed>.json (data-model §7).

Generations are stored one file per feed so a new generation rewrites only that feed's file,
not a multi-megabyte catalog (keeps the data repository history small).

History is never dropped: a feed that fails to load keeps its previous entries, a feed that
leaves the /feeds list is kept with listed=false, and a uid the API stops returning is kept
with present=false. The files carry no run timestamps, so an unchanged source rewrites nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .canonical import write_json_if_changed
from .gtfsdatajp import ApiError, FeedRecord, GenerationRecord, GtfsDataJpClient, Rejected
from .ids import is_path_id, require_path_id
from .licenses import normalize_license
from .ordering import GenerationRef, order_generations, rid_order_matches

FEEDS_SCHEMA = "gtfs-jp-monitor-catalog-feeds/1"
GENERATIONS_SCHEMA = "gtfs-jp-monitor-catalog-generations/1"

FeedKey = tuple[str, str]

# One-off happenings go to the run record. Everything else describes a lasting state of a feed
# and is stored as a note on its catalog entry, so a repeating state does not create a daily commit.
EVENT_CODES = frozenset({"FEED_FETCH_FAILED", "GENERATION_DISAPPEARED", "FEED_UNLISTED"})


@dataclass
class SyncResult:
    feeds_scanned: int = 0
    generations_seen: int = 0
    new_generations: list[tuple[str, str, str]] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)  # everything, for the console summary
    events: list[dict] = field(default_factory=list)  # EVENT_CODES only, for the run record
    notes: dict[FeedKey, set[str]] = field(default_factory=dict)
    rejected_feeds: list[dict] = field(default_factory=list)  # /feeds entries that could not be used
    changed_files: list[str] = field(default_factory=list)

    def warn(self, code: str, org_id: str | None = None, feed_id: str | None = None,
             uid: str | None = None, detail: str | None = None) -> None:
        item = {k: v for k, v in {"code": code, "org_id": org_id, "feed_id": feed_id, "uid": uid, "detail": detail}.items()
                if v is not None}
        self.warnings.append(item)
        if code in EVENT_CODES:
            self.events.append(item)
        elif is_path_id(org_id) and is_path_id(feed_id):
            self.notes.setdefault((org_id, feed_id), set()).add(code)
        else:
            self.rejected_feeds.append({k: v for k, v in item.items() if k != "detail"})


def feeds_path(data_dir: Path) -> Path:
    return Path(data_dir) / "catalog" / "feeds.json"


def generations_path(data_dir: Path, org_id: str, feed_id: str) -> Path:
    return Path(data_dir) / "catalog" / "generations" / require_path_id(org_id, "org_id") / f"{require_path_id(feed_id, 'feed_id')}.json"


def _load(path: Path, schema: str, key: str) -> list[dict]:
    if not path.is_file():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema") != schema:
        raise ValueError(f"{path}: unexpected schema {doc.get('schema')!r}")
    return list(doc[key])


def load_catalog(data_dir: Path) -> tuple[dict[FeedKey, dict], dict[FeedKey, dict[str, dict]]]:
    feeds = {(f["org_id"], f["feed_id"]): f for f in _load(feeds_path(data_dir), FEEDS_SCHEMA, "feeds")}
    gens: dict[FeedKey, dict[str, dict]] = {}
    root = Path(data_dir) / "catalog" / "generations"
    if root.is_dir():
        for org_dir in sorted(p for p in root.iterdir() if p.is_dir() and is_path_id(p.name)):
            for file in sorted(org_dir.glob("*.json")):
                if not is_path_id(file.stem):
                    continue
                for g in _load(file, GENERATIONS_SCHEMA, "generations"):
                    if (g["org_id"], g["feed_id"]) != (org_dir.name, file.stem):
                        raise ValueError(f"{file}: contains generations of another feed")
                    gens.setdefault((g["org_id"], g["feed_id"]), {})[g["uid"]] = g
    return feeds, gens


def _feed_entry(f: FeedRecord, listed: bool) -> dict:
    return {
        "org_id": f.org_id,
        "feed_id": f.feed_id,
        "feed_name": f.feed_name,
        "organization_name": f.organization_name,
        "feed_pref_id": f.feed_pref_id,
        "license": {"raw": f.license_raw, "id": normalize_license(f.license_raw)},
        "license_url": f.license_url,
        "is_discontinued": f.is_discontinued,
        "discontinued_date": f.discontinued_date,
        "latest_feed_start_date": f.latest_feed_start_date,
        "latest_feed_end_date": f.latest_feed_end_date,
        "memo": f.memo,
        "listed": listed,
        "notes": [],
    }


def _generation_entry(org_id: str, feed_id: str, g: GenerationRecord) -> dict:
    return {
        "org_id": org_id,
        "feed_id": feed_id,
        "uid": g.uid,
        "rid_observed": g.rid,
        "gtfs_url": g.gtfs_url,
        "from_date": g.from_date,
        "to_date": g.to_date,
        "published_at": g.published_at,
        "memo": g.memo,
        "present": True,
    }


def _refs(entries: Iterable[dict]) -> list[GenerationRef]:
    return [GenerationRef(e["uid"], e["from_date"], e["published_at"], e["rid_observed"]) for e in entries]


def _ordered_entries(by_uid: dict[str, dict]) -> list[dict]:
    ordered, unknown = order_generations(_refs(by_uid.values()))
    return [by_uid[r.uid] for r in ordered + unknown]


def _report_rejected(result: SyncResult, rejected: Iterable[Rejected]) -> None:
    for r in rejected:
        result.warn(r.code, r.org_id, r.feed_id, r.uid, r.detail)


def _merge_feed(
    result: SyncResult,
    key: FeedKey,
    fetched: tuple[GenerationRecord, ...],
    previous: dict[str, dict],
) -> dict[str, dict]:
    org_id, feed_id = key
    merged: dict[str, dict] = {}
    for g in fetched:
        merged[g.uid] = _generation_entry(org_id, feed_id, g)
        if g.uid not in previous:
            result.new_generations.append((org_id, feed_id, g.uid))
    for uid, old in previous.items():
        if uid in merged:
            continue
        if old.get("present", True):
            result.warn("GENERATION_DISAPPEARED", org_id, feed_id, uid, "uid no longer returned by the API; kept")
        merged[uid] = dict(old, present=False, rid_observed=None)

    present = [e for e in merged.values() if e["present"]]
    ordered, unknown = order_generations(_refs(present))
    if not rid_order_matches(ordered):
        result.warn("RID_ORDER_MISMATCH", org_id, feed_id, None, "API rid order differs from from_date/published_at order")
    for ref in unknown:
        result.warn("ORDERING_UNKNOWN", org_id, feed_id, ref.uid, "missing or invalid from_date")
    return merged


def sync_catalog(
    client: GtfsDataJpClient,
    data_dir: Path,
    only: set[FeedKey] | None = None,
    progress: Callable[[int, int, FeedKey], None] | None = None,
) -> SyncResult:
    """Refresh the catalog. `only` restricts which feeds are fetched; all other entries are kept as they are."""
    result = SyncResult()
    prev_feeds, prev_gens = load_catalog(data_dir)

    listed, rejected = client.list_feeds()
    for r in rejected:  # list-level rejects have no usable feed key; they never become notes
        item = {k: v for k, v in {"code": r.code, "org_id": r.org_id, "feed_id": r.feed_id, "detail": r.detail}.items() if v is not None}
        result.warnings.append(item)
        result.rejected_feeds.append({k: v for k, v in item.items() if k != "detail"})
    listed_by_key = {(f.org_id, f.feed_id): f for f in listed}

    feeds_out: dict[FeedKey, dict] = dict(prev_feeds)
    for key, f in listed_by_key.items():
        if only is not None and key not in only:
            continue
        entry = _feed_entry(f, listed=True)
        if entry["license"]["id"] is None:
            result.warn("UNKNOWN_LICENSE", key[0], key[1], None, f"raw license {f.license_raw!r}")
        feeds_out[key] = entry
    if only is None:
        for key, old in prev_feeds.items():
            if key not in listed_by_key:
                if old.get("listed", True):
                    result.warn("FEED_UNLISTED", key[0], key[1], None, "feed no longer in /feeds; kept")
                feeds_out[key] = dict(old, listed=False)

    gens_out: dict[FeedKey, dict[str, dict]] = {k: dict(v) for k, v in prev_gens.items()}
    to_fetch = sorted(k for k in listed_by_key if only is None or k in only)
    fetched_ok: set[FeedKey] = set()
    for index, key in enumerate(to_fetch):
        if progress:
            progress(index, len(to_fetch), key)
        try:
            fetched = client.get_generations(*key)
        except ApiError as err:
            result.warn("FEED_FETCH_FAILED", key[0], key[1], None, str(err)[:2000])
            continue  # previous entries stay untouched
        fetched_ok.add(key)
        result.feeds_scanned += 1
        result.generations_seen += len(fetched.generations)
        _report_rejected(result, fetched.rejected)
        if not fetched.complete:
            result.warn(
                "HISTORY_INCOMPLETE", key[0], key[1], None,
                f"api max_prev={fetched.reported_max_prev} max_next={fetched.reported_max_next}, "
                f"returned {len(fetched.generations)}",
            )
        gens_out[key] = _merge_feed(result, key, fetched.generations, prev_gens.get(key, {}))

    for key, entry in feeds_out.items():
        previous_notes = set(prev_feeds.get(key, {}).get("notes", []))
        current = result.notes.get(key, set())
        # A feed that was not fetched this run keeps the notes it had; license notes come from the list.
        entry["notes"] = sorted(current if key in fetched_ok else previous_notes | current)
    rejected = sorted({tuple(sorted(r.items())) for r in result.rejected_feeds})
    outputs = [(feeds_path(data_dir), {"schema": FEEDS_SCHEMA, "feeds": [feeds_out[k] for k in sorted(feeds_out)],
                                       "rejected": [dict(r) for r in rejected]})]
    for key in sorted(gens_out):
        outputs.append((generations_path(data_dir, *key),
                        {"schema": GENERATIONS_SCHEMA, "generations": _ordered_entries(gens_out[key])}))
    for path, doc in outputs:
        if write_json_if_changed(path, doc):
            result.changed_files.append(str(path.relative_to(data_dir)))
    return result
