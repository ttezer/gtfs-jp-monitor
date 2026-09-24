# Semantic engine — Part 2: comparison window and entity matching

Status: draft for review. All thresholds are placeholders in the engine configuration and are
set from pilot measurements; none is fixed in code.

## Goal

Before any change can be described, the engine must decide *which days* of the old and new
publication to compare, and *which* old stop, route, pattern and trip corresponds to which new
one. Identifiers are evidence, not identity: operators renumber IDs between publications.

Every match carries a `method` and a `confidence` in [0, 1]. Matches below the acceptance level
are kept as candidates but are not used as facts.

## 1. Service days and day types

For each publication, every `service_id` is expanded into its set of active dates:
`calendar.txt` weekday flags between `start_date` and `end_date`, plus `calendar_dates.txt`
additions, minus removals. Dates are clipped to the publication's validity
(`feed_info` start/end if present, else the catalogue `from_date`/`to_date`).

A **day** is described by the set of services active on it. Days are classified into day types
used throughout the report:

| Day type | Rule |
|---|---|
| `weekday` | Monday–Friday that is not a national holiday |
| `saturday` | Saturday that is not a national holiday |
| `sunday_holiday` | Sunday or national holiday |
| `special` | a date whose service set occurs on fewer than `special_max_days` dates in the window |

National holidays come from a bundled, versioned table of Japanese public holidays (a data file
of the engine, updated yearly). The table version is written into every output.

For each day type, the **typical day** of a period is the most frequent service set among its
dates (ties: the earliest date). Trips of a typical day define "the timetable" of that day type.

## 2. Comparison window

Planners ask two kinds of questions, answered by two modes chosen automatically:

1. **Same days (overlapping validity).** If the two publications share at least
   `min_overlap_days` valid dates, the comparison uses those shared dates only: *for the same
   calendar days, what did the old data say and what does the new data say?* Per day type, the
   typical day of each side within the shared dates is compared.
2. **Successive periods (no useful overlap).** Typical of yearly timetable changes
   (2025-04–2026-03 vs 2026-04–2027-03). Per day type, each side's **dominant** service set (the
   most frequent over its whole validity) is compared; the representative date is the last date
   of that set on the old side and the first on the new side. The last and first *periods* are
   not used: pilot data showed weekday periods split by school terms, where the last and first
   periods are school-holiday timetables rather than the regular one.

A **period** is a maximal date range over which a day type's typical service set does not change.
A publication that bundles two timetables (before and after a change) therefore has two periods;
they are listed in the report. A short bundled "before" timetable is not dominant, so it is never
mistaken for the regular timetable.

Days outside the compared dates are not silently dropped: their raw differences are labelled
`outside_comparison` (Part 1 accounting).

The chosen mode, the compared dates per day type and the periods of both sides are written into
the report header.

## 3. Stops

Stops are compared as **places**: a parent station with its platforms, or a stop without parent.

Matching, in order; each old place is matched at most once:

1. **Same `stop_id`** (or same parent `stop_id`): accepted when the name is equal after
   normalisation or the distance is at most `stop_same_id_max_m`. Otherwise it is a candidate
   only (the ID was reused for another place).
2. **Same normalised name within `stop_name_radius_m`**.
3. **Proximity with similar name**: within `stop_near_radius_m` and name similarity at least
   `stop_name_min_similarity`.

Name normalisation: Unicode NFKC, removal of spaces, and removal of a configurable list of
platform and direction suffixes (for example `（上り）`, `（下り）`, `のりば`, trailing platform
numbers). The list is part of the configuration so it can be tuned per pilot.

Similarity is the normalised edit similarity of the two names. Distance is great-circle distance.
The resulting relation is 1:1; unmatched places are `added` or `removed`.

## 4. Routes

A **line** is what a passenger recognises as one route: routes of a publication are grouped by
normalised `route_short_name`, falling back to `route_long_name`, then `route_id`.

Old and new lines are matched by:

1. **equal normalised name** — confidence 1;
2. otherwise **served places**: the Jaccard similarity of the places (Part 3 matches) served by
   the two lines' trips, at least `line_min_overlap`.

Matching may be 1:1 (same line, possibly renamed), N:1 (merged), 1:N (split) or N:M
(restructured); the shape is reported as found. Components larger than
`line_max_component` are not interpreted as merges or splits; their members stay unmatched
candidates, to avoid chains through shared corridors.

## 5. Patterns

A **pattern** is the ordered list of places visited by a trip, within a line and direction
(`direction_id`, or the order of first and last place when `direction_id` is missing).

Patterns of matched lines are matched by sequence similarity: the length of the longest common
subsequence of places divided by the longer length, at least `pattern_min_similarity`.
Differences between matched patterns are described as edits: places added or removed at an end
(extension, shortening) or in the middle (inserted, removed, detour).

## 6. Trips

Trips are matched within the same line, direction and day type of the compared typical days:

1. **Exact**: identical pattern and identical times at every place. When their IDs differ this
   is an ID change without service change.
2. **Assignment**: remaining trips are paired by lowest cost, where cost combines the median time
   difference at shared places (capped at `trip_max_shift_min`) and the pattern difference.
   Pairs above `trip_max_cost` are not made. Assignment is greedy in cost order, ties broken
   deterministically by old and new first departure and trip ID.
3. Unpaired old trips are `removed`, unpaired new trips are `added`.

A paired trip is `retimed` when times differ, `rerouted` when the pattern differs, or both.

## 7. Configuration

| Key | Meaning |
|---|---|
| `special_max_days` | service sets on fewer dates than this count as special days |
| `min_overlap_days` | shared valid dates needed for the "same days" mode |
| `stop_same_id_max_m`, `stop_name_radius_m`, `stop_near_radius_m` | stop matching distances |
| `stop_name_min_similarity` | minimum name similarity for proximity matches |
| `stop_name_suffixes` | suffixes removed during name normalisation |
| `line_min_overlap`, `line_max_component` | line matching |
| `pattern_min_similarity` | pattern matching |
| `trip_max_shift_min`, `trip_max_cost` | trip assignment |
| `accept_confidence` | below this, a match is only a candidate |

Values used are copied into every output.

## 8. Validation of this part

- Synthetic GTFS fixtures for each rule: renumbered stop IDs, moved stop, renamed stop, reused
  stop ID, renamed line, merged lines, split line, extended and shortened pattern, retimed trips,
  bundled two-period publication, successive-period comparison.
- Pilot feeds with manually checked expectations. Match quality (wrong and missed matches) is
  measured and reported, not assumed.
