# Semantic engine — Part 1: raw diff

Status: draft. Part 1 of the semantic engine (see also [data-model](../data-model.md)). Rules here
are derived from the GTFS and GTFS-JP specifications and from this project's requirements.

## Goal

List **every** difference between an old and a new GTFS ZIP, deterministically. Later layers
(entity matching, semantic events) explain these differences; nothing may be silently dropped.
The same two ZIPs and the same engine version always give byte-identical output.

## Input reading

1. Every member of the ZIP whose base name ends in `.txt` is a file, including unknown files.
   Members in a single top-level directory are accepted when the archive root has no `.txt`
   file; any other nesting is reported as `archive_layout` and the member is ignored.
   Member names containing `..`, absolute paths or backslashes are ignored and reported.
2. Decoding: UTF-8 (a leading BOM is removed); if that fails, CP932; if both fail, the file is
   compared by SHA-256 only (`undecodable`). The encoding used is recorded per file and side.
3. Parsing: RFC 4180 CSV. Header names are stripped of surrounding whitespace. Values are kept
   exactly as read — no trimming, case folding or number normalisation. A missing trailing
   field is the empty string. Fields beyond the header are ignored and counted per file
   (`ragged_rows`). Rows that are entirely empty are ignored. A file whose header repeats a
   column name is compared by SHA-256 only (`duplicate_header`).
4. Size guards: a file with more than `max_rows_per_file` rows is compared by row count and
   SHA-256 only (`too_large`); an archive whose declared uncompressed size exceeds
   `max_uncompressed_bytes` is rejected as a whole (configuration values).

## Row identity

Rows are matched by the primary key defined in the specification of the file:

| File | Primary key |
|---|---|
| agency.txt | agency_id |
| stops.txt | stop_id |
| routes.txt | route_id |
| trips.txt | trip_id |
| stop_times.txt | trip_id, stop_sequence |
| calendar.txt | service_id |
| calendar_dates.txt | service_id, date |
| fare_attributes.txt | fare_id |
| shapes.txt | shape_id, shape_pt_sequence |
| frequencies.txt | trip_id, start_time |
| transfers.txt | from_stop_id, to_stop_id, from_trip_id, to_trip_id, from_route_id, to_route_id (columns that exist) |
| feed_info.txt | single row |
| translations.txt | table_name, field_name, language, record_id, record_sub_id, field_value (columns that exist) |
| levels.txt | level_id |
| pathways.txt | pathway_id |
| attributions.txt | attribution_id |
| agency_jp.txt | agency_id |
| office_jp.txt | office_id |

A file falls back to **multiset** comparison (every row is compared as a whole; a changed row
appears as one removed and one added row) when:

- it is not in the table (including `fare_rules.txt`, whose specification makes every field part
  of the key, and `routes_jp.txt`),
- a key column is missing on either side, or
- a key value is duplicated on either side.

The reason for a fallback is recorded per file.

In keyed files, only columns present on both sides are compared cell by cell; a column present on
one side only is reported once as `column_added` / `column_removed` and its cells are not listed.

## Change kinds

| Kind | Meaning | Granularity |
|---|---|---|
| `file_added` / `file_removed` | file exists on one side only | one per file (rows not listed) |
| `column_added` / `column_removed` | column exists on one side only | one per column |
| `row_added` / `row_removed` | key (or whole row in multiset mode) exists on one side only | one per row |
| `field_changed` | same key, different value in a common column | one per cell |
| `file_changed_opaque` | `undecodable`, `too_large` or `duplicate_header` file whose SHA-256 differs | one per file |
| `rows_bulk` | row-level changes of one file exceed `bulk_threshold` | one per file, with counts |

`rows_bulk` replaces the row and cell entries of that file with counts (`added`, `removed`,
`changed_rows`, `changed_cells`); it still counts as one change, so the "every change is
reported" rule holds. Files whose rows later layers need (stops, routes, trips, stop_times,
calendar, calendar_dates, shapes, frequencies) are never bulked; the threshold only applies to
other files (for example very large fare tables).

## Determinism and identifiers

Changes are sorted by file name, then kind (in the table order above), then key tuple, then
column name. Each change gets the identifier `c` + its 1-based position, zero-padded to 7
digits (`c0000001`). The same inputs therefore give the same identifiers, which later layers use
as evidence references.

## Output

`gtfs-jp-semantic-rawdiff/1`, serialized canonically (data-model §5.1), stored gzip-compressed:

```json
{
  "schema": "gtfs-jp-semantic-rawdiff/1",
  "engine_version": "0.1.0",
  "config": { "bulk_threshold": 100000, "max_rows_per_file": 5000000 },
  "old": { "sha256": "<64 hex>" },
  "new": { "sha256": "<64 hex>" },
  "files": {
    "stops.txt": {
      "mode": "keyed",
      "key": ["stop_id"],
      "fallback_reason": null,
      "encoding": { "old": "utf-8", "new": "utf-8" },
      "rows": { "old": 401, "new": 402 },
      "counts": { "row_added": 1, "field_changed": 3 }
    }
  },
  "changes": [
    { "id": "c0000001", "file": "stops.txt", "kind": "row_added", "key": ["S100"], "new": { "stop_id": "S100", "stop_name": "…" } },
    { "id": "c0000002", "file": "stops.txt", "kind": "field_changed", "key": ["S1"], "column": "stop_name", "old": "A", "new": "B" }
  ],
  "archive_warnings": []
}
```

The output carries no timestamps. Values are source data and are not translated.

## Configuration

`bulk_threshold` and `max_rows_per_file` live in a versioned configuration file of the engine;
the values used are copied into every output. Initial values are placeholders to be set from
pilot measurements.

## Open points

- Where the full raw diff is stored (open decision).
- Memory use on the largest feeds (to be measured in the semantic pilot, Soru 19).
