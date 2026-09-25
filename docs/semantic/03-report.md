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

**Changes by date.** Every date both publications cover is also compared on its own, trip by
trip (a trip is its line, place sequence and times). The change of a date is compared with the
change of the typical day it follows: the typical day whose services it also runs, else the
typical day of its day type. Only the remainder is listed, dates with the same remainder in one
group; an added and a removed trip of one line departing within `trip_max_shift_min` of each
other are shown as one retimed trip. This catches special trips, holiday timetables and
short periods that the typical days never show. Trips that run on a shared date are accounted
as explained: their difference is listed here, or there is none.

### 3. Changes by line

One block per line that changed (unchanged lines are listed by name only):

- Status: added, discontinued, renamed (old name → new name), merged from / split into, changed.
- Trips per day type and per hour of first departure (hourly bands, 04–05 up to 27–28 h for
  service after midnight), old → new. Coarser groupings such as peak periods are built by the
  viewer from the hourly counts.
- First and last departure per direction and day type.
- Pattern changes per direction: places added, removed, inserted, detours, with the place names.
- **Route geometry** per direction on both sides, for every line: the most used shape of the
  direction, else the line through its stops, simplified to `report.geometry_tolerance_m`.
  Both lines are sampled every `report.geometry_sample_m` and each sample's distance to the
  other line is measured (up to `report.geometry_cap_m`); parts farther than
  `report.geometry_diverge_m` are where the route runs elsewhere and are kept as point ranges
  for drawing. A line whose route runs elsewhere counts as changed even if its stops and times
  did not change. The page draws a network map of all lines and, per line, old and new routes
  with the diverging parts.
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

Per pair of neighbouring, non-equivalent publications (data-model §11), under the feed's
directory in the data repository:

```text
changes/<engine_version>/<old_uid>__<new_uid>.report.json.gz   the report
changes/<engine_version>/<old_uid>__<new_uid>.error.json       marker when it could not be built
```

Reports are canonical JSON, gzip-compressed without timestamps (data-model §5.1). Raw
differences are not stored: section 6 keeps per-file counts and the unclassified ids, and
section 5 keeps row-level details up to `report.other_details_max` per topic; the full list is
rebuilt from the two ZIPs when needed.

Timetables are the largest part. To keep them compact:

- a timetable is stored once per side as place order plus a column per trip holding times as
  minutes after midnight (values above 1440 allowed for service after midnight);
- the difference view is **not stored**: it is built by the viewer from the two timetables and
  the stored trip pairing (a list of `[old_column, new_column]`, with `null` for added or
  removed);
- unchanged combinations store no timetable.

Pilot reports are 3–180 KB compressed. The web export indexes every report in the page and
writes the reports in one bundle per prefecture, which the page loads when a pair is opened.

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
- **Moved trips.** Trips left unmatched in their own line and direction are matched once more
  across all lines of the same day type (`moves`): the shorter place sequence must lie at least
  `cross_line_min_containment` inside the longer one, share its first or last place, and run
  within `cross_line_max_shift_min`. This finds route variants of one corridor that exchange
  trips (for example a trip extended from variant 30F to 30C) and trips whose direction key
  changed because they have no direction_id and their end points moved.
- **Pattern edits.** Collected from every rerouted trip pair of the line (and from its own
  moved trips), with the number of trips showing each edit; only when no paired trip changed
  route are the dominant patterns of both sides compared (`trips` is then null).
- **Trips by hour.** Per direction and day type.
- **Other changes.** Each topic carries up to `report.other_details_max` row-level items;
  shapes are summarised per shape_id.
- **All differences.** Every file of either side with its row counts, added / removed / changed
  rows and the accounting buckets, all counted in rows: a row with several changed fields counts
  once, in the least explained bucket of its fields; file and column changes are listed apart
  and count once each. The coverage header is the total of this table. Up to
  `report.other_details_max` unclassified differences are kept with their row-level details.
- **Reordered places.** A place that leaves a pattern in one edit and comes back in another is
  reported once as `reordered`.
- **Changed.** A line matched by name is `changed` when a trip pair in any compared direction and
  day type is not exact, or its dominant pattern changed; otherwise `unchanged` and its trips,
  first/last and timetables are omitted.
- **Timetable rows.** The distinct place sequences of one side, most frequent first, merged into
  one row order; a loop visiting a place twice gets two rows.
- **Accounting.**

  | Raw difference | Bucket |
  |---|---|
  | a field whose old and new text are the same number or time (`140.5208490` / `140.520849`) | explained, listed under Other changes as formatting |
  | `calendar.txt` | explained (service days) |
  | fares, calendar exceptions, agency, office, translations, feed info, shapes, transfers, other files | explained, listed under Other changes |
  | `stops.txt` row of a place that is not unchanged, or whose id changed | explained |
  | `routes.txt` row of a line that is not unchanged, or a renumbered route of an unchanged line | explained |
  | `trips.txt` / `stop_times.txt` row of a compared trip that changed, or only changed id | explained |
  | `stop_times.txt` stop_id change where old and new stop belong to the same matched place | explained (renumbering) |
  | `stop_times.txt` row of a trip paired exactly with itself (same places and times) | explained (stop_sequence renumbered) |
  | `trips.txt` route_id change of such a trip between two routes of the same line | explained (route renumbered) |
  | a non-core field of stops, routes, trips or stop times (headsign, pickup / drop-off, timepoint, shape link, codes, ...), a stop coordinate change below `stop_moved_min_m`, or a non-core column added / removed | explained, listed under Other changes as attributes, grouped by line and old → new value |
  | a trips or stop times row of a trip that ran on no compared day | outside comparison |
  | `frequencies.txt`; a core column or file added / removed; any other core-field change nothing explains | unclassified |

  Core fields are the ones the place, line and trip layers read: stop id, name, coordinates,
  parent and location type; route id and names; trip id, route, service and direction; stop
  times' trip, sequence, stop and times.

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
