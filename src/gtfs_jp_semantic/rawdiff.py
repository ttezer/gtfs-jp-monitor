"""Raw diff between two GTFS feeds (docs/semantic/01-raw-diff.md).

Every difference is listed once, in a fixed order, with a positional identifier.
"""

from __future__ import annotations

import collections
import gzip
import io
from pathlib import Path

from gtfs_jp_monitor.canonical import dumps

from . import ENGINE_VERSION
from .reader import Config, Feed, Table, read_feed

SCHEMA = "gtfs-jp-semantic-rawdiff/1"

# Primary keys from the GTFS / GTFS-JP specifications. None = single-row file.
FIXED_KEYS: dict[str, tuple[str, ...] | None] = {
    "agency.txt": ("agency_id",),
    "stops.txt": ("stop_id",),
    "routes.txt": ("route_id",),
    "trips.txt": ("trip_id",),
    "stop_times.txt": ("trip_id", "stop_sequence"),
    "calendar.txt": ("service_id",),
    "calendar_dates.txt": ("service_id", "date"),
    "fare_attributes.txt": ("fare_id",),
    "shapes.txt": ("shape_id", "shape_pt_sequence"),
    "frequencies.txt": ("trip_id", "start_time"),
    "levels.txt": ("level_id",),
    "pathways.txt": ("pathway_id",),
    "attributions.txt": ("attribution_id",),
    "agency_jp.txt": ("agency_id",),
    "office_jp.txt": ("office_id",),
    "feed_info.txt": None,
}
# Keys made of whichever of these columns exist on both sides.
OPTIONAL_KEYS: dict[str, tuple[str, ...]] = {
    "transfers.txt": ("from_stop_id", "to_stop_id", "from_trip_id", "to_trip_id", "from_route_id", "to_route_id"),
    "translations.txt": ("table_name", "field_name", "language", "record_id", "record_sub_id", "field_value"),
}
# Later layers need row-level evidence from these; they are never aggregated.
NEVER_BULK = frozenset({
    "stops.txt", "routes.txt", "trips.txt", "stop_times.txt", "calendar.txt",
    "calendar_dates.txt", "shapes.txt", "frequencies.txt",
})
KIND_ORDER = {k: i for i, k in enumerate((
    "file_added", "file_removed", "column_added", "column_removed", "row_added", "row_removed",
    "field_changed", "file_changed_opaque", "rows_bulk",
))}


def _row_dict(header: tuple[str, ...], row: tuple[str, ...]) -> dict[str, str]:
    return dict(zip(header, row))


def _choose_key(name: str, old: Table, new: Table) -> tuple[tuple[str, ...] | None, str | None]:
    """(key columns, fallback reason). key () means single-row file; None means multiset."""
    both = set(old.header) & set(new.header)
    if name in FIXED_KEYS:
        key = FIXED_KEYS[name]
        if key is None:
            if old.row_count <= 1 and new.row_count <= 1:
                return (), None
            return None, "multiple_rows_in_single_row_file"
        if not all(c in both for c in key):
            return None, "key_column_missing"
    elif name in OPTIONAL_KEYS:
        key = tuple(c for c in OPTIONAL_KEYS[name] if c in both)
        if not key:
            return None, "key_column_missing"
    else:
        return None, "no_primary_key"
    for table in (old, new):
        idx = [table.header.index(c) for c in key]
        keys = [tuple(r[i] for i in idx) for r in table.rows]
        if len(set(keys)) != len(keys):
            return None, "duplicate_key"
    return key, None


def _keyed(name: str, old: Table, new: Table, key: tuple[str, ...]) -> list[dict]:
    changes: list[dict] = []
    oi = [old.header.index(c) for c in key]
    ni = [new.header.index(c) for c in key]
    old_rows = {tuple(r[i] for i in oi): r for r in old.rows}
    new_rows = {tuple(r[i] for i in ni): r for r in new.rows}
    common = sorted(set(old.header) & set(new.header))
    o_pos = {c: old.header.index(c) for c in common}
    n_pos = {c: new.header.index(c) for c in common}
    for k in old_rows.keys() - new_rows.keys():
        changes.append({"file": name, "kind": "row_removed", "key": list(k), "old": _row_dict(old.header, old_rows[k])})
    for k in new_rows.keys() - old_rows.keys():
        changes.append({"file": name, "kind": "row_added", "key": list(k), "new": _row_dict(new.header, new_rows[k])})
    for k in old_rows.keys() & new_rows.keys():
        o, n = old_rows[k], new_rows[k]
        for c in common:
            if o[o_pos[c]] != n[n_pos[c]]:
                changes.append({"file": name, "kind": "field_changed", "key": list(k), "column": c,
                                "old": o[o_pos[c]], "new": n[n_pos[c]]})
    return changes


def _multiset(name: str, old: Table, new: Table) -> list[dict]:
    columns = sorted(set(old.header) | set(new.header))

    def as_tuple(table: Table, row: tuple[str, ...]) -> tuple[str, ...]:
        d = _row_dict(table.header, row)
        return tuple(d.get(c, "") for c in columns)

    old_c = collections.Counter(as_tuple(old, r) for r in old.rows)
    new_c = collections.Counter(as_tuple(new, r) for r in new.rows)
    changes: list[dict] = []
    for row, n in (old_c - new_c).items():
        changes.extend({"file": name, "kind": "row_removed", "key": None, "old": dict(zip(columns, row)), "_sort": row}
                       for _ in range(n))
    for row, n in (new_c - old_c).items():
        changes.extend({"file": name, "kind": "row_added", "key": None, "new": dict(zip(columns, row)), "_sort": row}
                       for _ in range(n))
    return changes


def _sort_key(change: dict) -> tuple:
    key = change.get("_sort") or tuple(change.get("key") or ())
    return (change["file"], KIND_ORDER[change["kind"]], key, change.get("column", ""))


def diff_feeds(old: Feed, new: Feed, config: Config) -> dict:
    files: dict[str, dict] = {}
    changes: list[dict] = []
    for name in sorted(set(old.tables) | set(new.tables)):
        o, n = old.tables.get(name), new.tables.get(name)
        meta = {
            "mode": None, "key": None, "fallback_reason": None,
            "encoding": {"old": o.encoding if o else None, "new": n.encoding if n else None},
            "status": {"old": o.status if o else None, "new": n.status if n else None},
            "rows": {"old": o.row_count if o else None, "new": n.row_count if n else None},
            "ragged_rows": {"old": o.ragged_rows if o else 0, "new": n.ragged_rows if n else 0},
        }
        file_changes: list[dict] = []
        if o is None or n is None:
            meta["mode"] = "presence"
            kind, table = ("file_added", n) if o is None else ("file_removed", o)
            file_changes.append({"file": name, "kind": kind, "rows": table.row_count})
        elif o.status != "ok" or n.status != "ok":
            meta["mode"] = "opaque"
            if o.sha256 != n.sha256:
                file_changes.append({"file": name, "kind": "file_changed_opaque",
                                     "old_sha256": o.sha256, "new_sha256": n.sha256})
        else:
            for c in sorted(set(n.header) - set(o.header)):
                file_changes.append({"file": name, "kind": "column_added", "column": c})
            for c in sorted(set(o.header) - set(n.header)):
                file_changes.append({"file": name, "kind": "column_removed", "column": c})
            key, reason = _choose_key(name, o, n)
            meta["key"], meta["fallback_reason"] = (list(key) if key is not None else None), reason
            meta["mode"] = "keyed" if key is not None else "multiset"
            rows = _keyed(name, o, n, key) if key is not None else _multiset(name, o, n)
            changed_rows = len({tuple(c["key"]) for c in rows if c["kind"] == "field_changed"})
            row_level = sum(c["kind"] in ("row_added", "row_removed") for c in rows) + changed_rows
            if name not in NEVER_BULK and row_level > config.bulk_threshold:
                meta["mode"] = "bulk"
                kinds = collections.Counter(c["kind"] for c in rows)
                rows = [{"file": name, "kind": "rows_bulk", "counts": {
                    "added": kinds["row_added"], "removed": kinds["row_removed"],
                    "changed_rows": changed_rows, "changed_cells": kinds["field_changed"]}}]
            file_changes.extend(rows)
        meta["counts"] = dict(sorted(collections.Counter(c["kind"] for c in file_changes).items()))
        files[name] = meta
        changes.extend(file_changes)

    changes.sort(key=_sort_key)
    for index, change in enumerate(changes, start=1):
        change.pop("_sort", None)
        change["id"] = f"c{index:07d}"
    warnings = [dict(w, side=side) for side, feed in (("old", old), ("new", new)) for w in feed.warnings]
    return {
        "schema": SCHEMA,
        "engine_version": ENGINE_VERSION,
        "config": config.as_dict(),
        "old": {"sha256": old.sha256},
        "new": {"sha256": new.sha256},
        "files": files,
        "changes": changes,
        "archive_warnings": warnings,
    }


def diff_zips(old_zip: Path, new_zip: Path, config: Config | None = None) -> dict:
    config = config or Config.load()
    return diff_feeds(read_feed(old_zip, config), read_feed(new_zip, config), config)


def gzip_bytes(doc: dict) -> bytes:
    """Canonical JSON, gzip-compressed without timestamp or file name, so bytes are reproducible."""
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as handle:
        handle.write(dumps(doc).encode("utf-8"))
    return buffer.getvalue()
