# Semantic engine — Part 3: change report for planners

Status: draft for review.

## Reader and purpose

The reader is a **transport planner** comparing two publications of one feed. The report answers,
in this order: *what changed in the service*, *where exactly*, and *is the data itself sound*.
Validation results support the report; they do not lead it.

One report is produced for every consecutive pair of publications (data-model §9 pairs), using
the comparison window and matches of Part 2. Equivalent pairs (data-model §4.2) produce no
report; they carry only the equivalence flag.

## Sections

### 0. Header

- Feed, organisation, prefecture; old and new publication (validity, publication date, the
  source's note text).
- Comparison mode (same days / successive periods), compared dates per day type, and the periods
  found in each publication.
- Notes that change how the report should be read: format difference (GTFS-JP extension files),
  possible change of scope (other feeds of the organisation ended), equivalence.
- Engine version, configuration and holiday-table versions; coverage (share of raw differences
  explained, Part 1).

### 1. Summary

Counts a planner can scan in seconds:

| Topic | Values |
|---|---|
| Lines | added, discontinued, renamed, merged / split, changed, unchanged |
| Places (stops) | added, removed, renamed, moved (with distance) |
| Trips per day type | old → new, and difference, for weekday / saturday / sunday_holiday |
| First / last departure | lines whose first or last trip moved by at least `first_last_min_shift` |
| Fares | changed yes/no, with counts |
| Data quality | Publish and Overall score, old → new |

### 2. Service days

Per day type: compared dates, number of active days in each publication's validity, and the
periods of both publications (with their date ranges). Special days are listed with their dates.

### 3. Changes by line

One block per line that changed (unchanged lines are listed by name only):

- Status: added, discontinued, renamed (old name → new name), merged from / split into, changed.
- Trips per day type and per hour of first departure (hourly bands, 04–05 up to 27–28 h for
  service after midnight), old → new. Coarser groupings such as peak periods are built by the
  viewer from the hourly counts.
- First and last departure per direction and day type.
- Pattern changes per direction: places added, removed, inserted, detours, with the place names.
- **Timetables** per direction and day type, in three views:
  - **old**: the old typical day's trips as a table, places in pattern order as rows, trips as
    columns ordered by first departure;
  - **new**: the same for the new side;
  - **difference**: one table over both sides, where every column is a matched trip pair, an
    added trip or a removed trip. A retimed cell shows the new time with the old time struck
    through; added and removed trips are marked as such; places served on one side only are
    marked in their row.

  Only line/direction/day-type combinations that changed get timetables; unchanged ones are
  listed.

### 4. Places

Added, removed, renamed (old → new name) and moved places (distance in metres), each with the
lines that serve it.

### 5. Other changes

Grouped raw differences that are not about lines or places: fares (changed fare classes and
prices), calendar exceptions outside the compared days, agency and office information,
translations, feed information. Each item links to its raw differences.

### 6. All differences

Per file: counts of added, removed and changed rows; the classified share; and the list of
**unclassified** differences. Nothing is dropped (Part 1).

### 7. Data quality

The validation diff (scores, rule counts) already produced by the monitoring pipeline.

## Storage

Per consecutive, non-equivalent pair, under the feed's directory in the data repository:

```text
changes/<engine_version>/<old_uid>__<new_uid>.report.json.gz   sections 0–5 and 7, and 6 as counts
changes/<engine_version>/<old_uid>__<new_uid>.raw.json.gz      Part 1 raw differences (section 6 detail)
```

Both files are canonical JSON, gzip-compressed without timestamps (data-model §5.1).

Timetables are the largest part. To keep them compact:

- a timetable is stored once per side as place order plus a column per trip holding times as
  minutes after midnight (values above 1440 allowed for service after midnight);
- the difference view is **not stored**: it is built by the viewer from the two timetables and
  the stored trip pairing (a list of `[old_column, new_column]`, with `null` for added or
  removed);
- unchanged combinations store no timetable.

Size is measured on pilot feeds before full production; if the repository approaches 1 GB,
older pairs' raw files move to release assets first (the report stays in git).

## Language

Everything stored is language-neutral: codes, numbers, dates, and names as published (place,
line and feed names are never translated). Section titles, labels and explanations come from the
viewer's TR / EN / JA catalogue.

## Builder

`gtfs_jp_semantic.report.build_report` assembles the report; `gtfs_jp_semantic.accounting`
places every raw difference in one bucket.

- **Line entries.** One entry per line match (Part 2 §4). Its key is the new line key, or the
  joined keys of a merge target / split result (`A+B`), or the old key when discontinued; a
  collision gets a `~2` suffix. The lines of a merge or split are one entry, so `related` stays
  empty until entries are split per line.
- **Changed.** A line matched by name is `changed` when a trip pair in any compared direction and
  day type is not exact, or its dominant pattern changed; otherwise `unchanged` and its trips,
  first/last and timetables are omitted.
- **Timetable rows.** The distinct place sequences of one side, most frequent first, merged into
  one row order; a loop visiting a place twice gets two rows.
- **Accounting.**

  | Raw difference | Bucket |
  |---|---|
  | `calendar.txt` | explained (service days) |
  | fares, calendar exceptions, agency, office, translations, feed info, shapes, transfers, other files | explained, listed under Other changes |
  | `stops.txt` row of a place that is not unchanged, or whose id changed | explained |
  | `routes.txt` row of a line that is not unchanged, or a renumbered route of an unchanged line | explained |
  | `trips.txt` / `stop_times.txt` row of a compared trip that changed, or only changed id | explained |
  | the same for a trip that ran on no compared day | outside comparison |
  | `frequencies.txt`; file or column changes of stops, routes, trips, stop times; anything else | unclassified |

## Report schema

`schemas/semantic-report.schema.json` (`gtfs-jp-semantic-report/1`) is binding; an example is in
`tests/fixtures/semantic/report-example.json`. Its top level follows the sections above:
`header`, `summary`, `service_days`, `places`, `lines`, `other`, `accounting`, `quality`.

- Every place is defined once in `places`; lines, pattern edits and timetables refer to places by
  index.
- Reading notes are codes with parameters (`FORMAT_DIFFERENCE`, `POSSIBLE_SCOPE_CHANGE`, ...);
  the viewer renders their text.
- Times are minutes after midnight of the service day (values above 1440 after midnight).
- References JSON Schema cannot express (indices in range, one time per timetable row, every
  trip in `pairs` exactly once, coverage parts adding up) are checked by
  `gtfs_jp_semantic.report_check.check_report` before a report is written.
