# Data model and pipeline contracts

This document is the reference for the rules the code implements. Section numbers are cited
in code comments and schemas as `data-model §N`.

## §1 Sources and archive policy

- Feed metadata and generation ZIPs come from the gtfs-data.jp API v2.
- ZIPs are downloaded temporarily, analysed and deleted; they are never stored. A generation
  can be analysed again later by fetching it through its permanent `gtfs_file_uid`.
- Every stored analysis records the ZIP SHA-256. A later download with a different hash is
  reported as `SOURCE_CHANGED`; the stored result is kept. A generation that can no longer be
  downloaded is `SOURCE_UNAVAILABLE`; its stored results are kept.
- `gtfs_url` redirects to a signed storage URL that carries temporary credentials. That URL is
  followed but never logged, returned or stored.

## §2 Generation identity and ordering

- The permanent identifier of a generation is `gtfs_file_uid` (lower-case UUID v4). The API's
  `rid` (`next_N`, `current`, `prev_N`) is relative and shifts over time; it never appears in
  analysis records or diff keys, only in the catalog and run records.
- Generations of a feed are ordered oldest first by `from_date`, then `published_at` compared
  as an instant, then `uid`. A missing or unparsable `published_at` sorts first within its day.
  `published_at` never leads: bulk re-uploads give many old generations the same value.
- A generation without a usable `from_date` is not ordered (`ORDERING_UNKNOWN`).
- The API's own rid order is checked against this order; a mismatch is a warning and this
  order stays canonical.

## §3 Identifier validation

`org_id` and `feed_id` must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` (and are not `.` or
`..`); `gtfs_file_uid` must be a lower-case UUID v4. Values from the API are validated before
any path is built; paths are built only from validated components.

## §4 Data repository layout

```text
catalog/feeds.json
catalog/generations/<org_id>/<feed_id>.json
feeds/<org_id>/<feed_id>/feed.json
feeds/<org_id>/<feed_id>/generations/<uid>/<analysis_key>.json
feeds/<org_id>/<feed_id>/generations/<uid>/content.json
feeds/<org_id>/<feed_id>/diffs/<analysis_key>/<old_uid>__<new_uid>.json
feeds/<org_id>/<feed_id>/changes/<engine_version>/<old_uid>__<new_uid>.report.json.gz
feeds/<org_id>/<feed_id>/changes/<engine_version>/<old_uid>__<new_uid>.error.json
runs/<YYYY>/<run_id>.json
```

`analysis_key` is `<release_tag>__<gtfs_jp_profile>` (for example `v0.14.0__auto`). Results of
different releases or profiles live side by side and are never compared with each other.

### §4.1 Commit policy

- A run that changes no data writes no file, so it produces no commit.
- Run records are written only when data changed or a warning or failure occurred.
- Time-dependent values (run times, durations, observed `rid`) appear only in run records and
  the catalog, never in generation or diff files.

### §4.2 Equivalent publications

Some feeds republish unchanged timetables as new generations (for example daily automatic
imports). A publication is *equivalent to the previous one* when both of these are identical:

- its analysis summary for the same analysis key: validation status, partial details,
  publishability, coverage, GTFS-JP detection, scores, metrics, file row counts and rule counts;
- the content signature of its ZIP (`content.json`, `schemas/content.schema.json`), which does
  not depend on the analysis key.

The summary alone is not enough: stop names, coordinates, times and fares can change while every
count and rule result stays the same. The signature ignores how the ZIP is packed (member order,
compression, BOM, line endings, CSV quoting, column and row order) but never merges different
values: nothing is trimmed, case-folded, rounded or decoded lossily, and a file that is not
regular UTF-8 CSV is compared byte for byte (`src/gtfs_jp_monitor/signature.py`). A publication
without a signature for its analysed ZIP is never equivalent. The analysis run signs every ZIP
it downloads and, up to a limit per run, publications analysed before signatures existed whose
summary equals a neighbour's.

The ZIP bytes may still differ. Equivalence is recorded as `equivalent_to_previous` in
`feed.json`; no publication is removed. Views may merge equivalent publications, but must always say that they did and keep the
merged publications reachable.

## §5 Generation record

`schemas/generation.schema.json` is binding. The record is language-neutral: rule codes,
severity and class codes, numbers, dates and the source's own names; no titles, messages or
other generated text. Validation dates are always the generation's `from_date` (§6.1).

### §5.1 Canonical serialization

All JSON files are UTF-8 without BOM, keys sorted, 2-space indent, no ASCII escaping, and end
with one newline. Scores and averages are rounded to one decimal place, halves away from zero;
deltas are computed from the stored rounded values and rounded the same way. Gzip output is
written without timestamp or file name.

## §6 Analyzer invocation

`gtfs-analyzer validate <zip> --json --today YYYYMMDD --gtfs-jp-profile <auto|v3|v4> -o <file>`

- Exit `0` (no notices) and `1` (notices) are successful analyses; `2` is fatal. A fatal exit
  must come with a fatal report and vice versa.
- Scores are read from `reports.r5`, the publishability verdict from `reports.r1`, counts
  from `metrics`, rule counts from `notices`.
- `PARTIAL` results keep their scores and record which files, stages or checks were skipped.

### §6.1 Validation date

`validate_date = from_date` of the generation. It never depends on the run date, so the same
generation always gets the same result, and consecutive generations are compared as they were
on their first service day.

### §6.2 Pinned binary and platform

Only binaries installed from the release archive pinned in `analyzer.lock.json` (verified by
SHA-256 and marked at installation) may write to the data repository. Results for the data
repository are produced on `x86_64-linux` only, because the same analyzer code has been seen to
give different results on different platforms. Unpinned binaries may write only into a directory
that contains a `.unpinned-scratch` marker file.

### §6.3 GTFS-JP profile

The requested profile is part of the analysis identity. The default is `auto`; `v3` and `v4`
can be selected per run.

## §7 Catalog sync

- One request lists all feeds; one request per feed returns its whole history
  (large `max_prev` and `max_next`; the returned counts are checked against the reported ones).
- Contact fields (e-mail addresses) are never copied.
- History is never dropped: a feed that fails to load keeps its previous entries, a feed that
  leaves the list is kept with `listed: false`, a uid the API stops returning is kept with
  `present: false`.
- An unchanged source leaves every catalog file byte-identical. A `rid` shift is not a new
  generation.
- Lasting conditions of a feed (rid order mismatch, unknown license, rejected API records,
  incomplete history, unordered generations) are stored as `notes` on its catalog entry and
  change only when the condition changes. One-off events (fetch failure, a generation that
  disappears, a feed that leaves the list) go to the run record.

## §8 Incremental analysis

- A generation is pending while it has no record for the current analysis key.
- Pending generations are processed newest first, round-robin across feeds, so an interrupted
  backfill still covers the current period of every feed. Existing records act as checkpoints.
- Transient download failures write nothing and are retried next run. Analyzer-side failures
  (fatal report, timeout, crash, invalid report) are stored as `FATAL` records.

### §8.1 Analyzer upgrades

A new analyzer release or profile is a new analysis key; results of different keys are never
compared (§4). An upgrade does not switch the site piece by piece:

1. The new key is filled in the background, newest publications first, while the site and the
   reports stay on the current key.
2. When every publication the page shows (§11 window) has a result under the new key, or a
   lasting failure (`FATAL`, `SOURCE_UNAVAILABLE`), the export switches to the new key in one
   commit.
3. Older publications keep being analysed under the new key afterwards.

The switch can change which publications are equivalent or `FATAL`, so report pairs are
recomputed; content signatures do not depend on the key and keep their value. During the fill a
run analyses under two keys, so its time budget is planned for both.

## §9 Validation diffs

- `schemas/diff.schema.json` is binding; `build_diff` in `src/gtfs_jp_monitor/diff.py` is the
  reference algorithm. Any client comparing two generations must reproduce it.
- Stored diffs cover consecutive pairs only: neighbours in §2 order that both have a `COMPLETE`
  analysis for the key. Generations in between without one are listed in `skipped_uids`;
  unordered generations never take part.
- Both sides must share the analyzer identity (version, release, binary hash, platform,
  profile). When a generation is inserted between two others, the stale pair file is removed.

## §10 Scheduled workflow

`.github/workflows/gtfs-jp-monitor.yml` runs daily at 04:10 JST and on demand: unit tests,
pinned analyzer installation, catalog sync, incremental analysis, semantic change reports
(§11), and one commit to the data repository when something changed. A single concurrency
group guarantees one writer at a time.

After the data commit the job rebuilds the public web site from the data repository
(`export-web`, one gzip-compressed file per report) and a second job deploys it to GitHub Pages;
only that job has `pages: write` and `id-token: write`.

### §10.0 Site files

`export-web` writes the public site:

```text
index.html                                          the page with its data embedded
status.json                                         status and sizes (§10.2)
metrics.json                                        metrics of the last 365 days (§13), loaded after the page
fields.json.gz                                      field filling per publication (§15), loaded after the page
reports/index.json.gz                               summary of every report, loaded after the page
reports/<org_id>/<feed_id>/<old_uid>__<new_uid>.json.gz   one semantic report (§11)
```

The embedded data keeps per publication only the count of each rule; the rule's severity and
class are stored once in `rule_meta`, and a publication repeats them only where they differ.
Report files are the machine-readable form of the reports, at a stable address per pair.

### §10.1 Page links

The page keeps its view in the URL fragment, so a comparison can be shared:
`#feed=<org_id>/<feed_id>&old=<uid>&new=<uid>&tab=<overview|history|reports|gtfsjp>&view=<report|verify>`.
Every part is optional; defaults (`overview`, `report`) are left out. Links hold publication
uids, never positions, so they keep working when publications are merged or regrouped:

- the old publication is written as the last one of its group and the new one as the first of
  its group, as a report names its pair (§11);
- a link whose `old` is newer than `new` is turned round;
- a uid inside a merged group opens that group;
- a feed or uid the page does not have leaves the view unchanged and says the link was ignored.

The page rewrites the fragment with `history.replaceState` as the view changes, so browsing adds
no history entries. Changing this format breaks shared links.

### §10.2 Status and storage

`export-web` embeds a `status` block in the page and writes the same as `status.json` next to
it, plus `site_bytes`:

- `built_at`: when the site was built. The site is rebuilt after every run, so this is the
  heartbeat; run records (§4.1) are written only when data changed and cannot serve as one.
  `last_run` is the newest run record.
- `backlog`: publications not analysed yet for the key and report pairs still pending (§11).
- `storage`: sizes of the data working tree, reports (count, total, average, largest),
  analyses, content signatures and diffs.

The page shows how long ago the site was built and marks it stale after 36 hours.
`check-storage` writes the sizes to the job summary and warns past 75% of a budget: the
project's own budget of 1 GiB for the data repository with its history (read from the GitHub
API; a shallow checkout cannot measure it) and the 1 GiB GitHub Pages limit for the site.
Deleting files does not shrink the history, so stored data is budgeted by its growth.

A watchdog workflow in the data repository (private, and committed to every night, so it is not
disabled for inactivity like a public repository's schedule) reads the published `status.json`
daily and fails when `built_at` is more than 30 hours old; GitHub then notifies by e-mail. It
catches a monitoring workflow that fails and one that does not start at all.

## §11 Semantic change reports

- Reports cover only the publications the page shows: grouped as the page groups them, from
  three groups before the current one to the newest (`window_uids`), plus neighbouring pairs
  whose new publication was published in the last 60 days (`RECENT_DAYS`), so a feed that
  publishes daily loses no change when runs stop for a few days. Older history is analysed (§8)
  but gets no report; reports already stored are kept, and a pair that leaves the window keeps
  its report.
- One report per pair of neighbouring publications as the web page shows them: analysed for
  the key and not `FATAL` (`PARTIAL` counts, since the engine reads the ZIP, not the validation
  result); unanalysed publications are passed over, a `FATAL` one breaks the chain, and
  equivalent pairs (§4.2) get no report. `report_pairs` in `src/gtfs_jp_monitor/changes.py`
  is the reference.
- Pairs the page can compare beyond neighbours are reported as well: every non-neighbouring
  pair of the window without a `FATAL` side (`page_pairs`). They are queued after the
  two newest rounds of neighbour pairs.
- Both ZIPs are downloaded again and must match the analysed SHA-256; reports are produced on
  the production platform only, like analyses (§6.2).
- A pair that cannot be reported (source unavailable or changed, engine error) gets an
  `.error.json` marker and is not retried for that engine version; an engine error is retried
  once the engine code changes (the marker records an `engine_build` digest of it); a transient download
  failure writes nothing and is retried next run. A new engine version reports every pair
  again under its own directory.
- Raw differences (docs/semantic/01-raw-diff.md) are not stored; they can be rebuilt from the
  two ZIPs. Newest pairs are reported first, round-robin across feeds, at most
  `report_limit` per run and within a time budget (`report_minutes`): no new report starts
  after it, so the job never reaches its own time limit and loses the run.
- Each report is built in its own process, with a time limit (15 min) and, on Linux, a memory
  limit (8 GiB): memory is returned after every pair, and a pair beyond the limits fails alone
  with an engine-error marker instead of ending the run. The step logs one line per pair.

## §12 Pair classification

Every exported publication after the first carries the class of the pair it forms with the
publication before it (`pair` in the page data; `src/gtfs_jp_monitor/classify.py`):

| Code | Class | When |
|---|---|---|
| `E` | `EQUIVALENT` | Same analysis summary and content signature (§4.2); no report is built |
| `M` | `MEANINGFUL_SERVICE_CHANGE` | The report has at least one passenger-facing change |
| `T` | `TECHNICAL_OR_METADATA_ONLY` | The report has changes, none passenger-facing |
| `~M`, `~T` | estimated | No report exists, but both publications have content records: `~T` when only `feed_info.txt`, `calendar.txt` and `calendar_dates.txt` differ, else `~M`. On reported pairs this agreed with the report for 499 of 501 technical and 43 of 44 meaningful cases (2026-09-27) |
| `U/<reason>` | `UNKNOWN` | No report says what changed: `U/FATAL` (either side failed validation), `U/NOT_REPORTED` (outside the reported window, §11, or not built yet), or the report's error code (`U/SOURCE_UNAVAILABLE`, `U/SOURCE_CHANGED`, `U/ENGINE_ERROR`) |

The first publication of a feed forms no pair, so rates over pairs use one fewer than the
number of publications. `format_transition: true` marks a pair whose GTFS-JP extension files
differ; validation results across it are not like for like. Results under different analysis
keys are never compared (§4).

Which report changes are passenger-facing is set in `src/gtfs_jp_monitor/classification.json`:

- every change of lines and places, trips per day type, first and last departures, trips moved
  between lines, date changes and fares;
- the topics fares, translations, transfers, agency, office and other files; the calendar
  change `service_dates_changed`;
- non-core columns listed as passenger-facing (for example `stop_headsign`, `pickup_type`,
  `drop_off_type`, `trip_headsign`, `route_color`).

Technical are `feed_info`, formatting, `shapes` (a route that runs elsewhere already counts as a
changed line), the calendar changes outside the period both publications share (usually a
longer validity), unused or ineffective calendar entries, and non-core columns listed as
technical (`shape_id`, `block_id`, `timepoint`, `shape_dist_traveled`, ...). Anything the file does
not list, and unclassified report rows, count as passenger-facing, so an unknown change is never
hidden as technical; a test fails when the engine can emit a topic, summary item or calendar
kind the file does not place. The rules are applied to stored reports when the site is built,
so changing them needs no new reports. The report index carries each report's class with its
reasons, the rule codes that decided it.

## §13 Metrics over time

`export-web` writes `metrics.json` (`src/gtfs_jp_monitor/metrics.py`) per feed and in total, over
the last 365 days by publication date. It carries its window (`from`, `to`), the analysis key
and the engine version. Pairs are those of §12 whose newer publication falls in the window.

| Metric | Definition |
|---|---|
| `publication_frequency` | Publications in the catalog per 30 days, analysed or not |
| `meaningful_update_frequency` | `M` and `~M` pairs per 30 days |
| `equivalent_republication_ratio` | `E` / pairs with a known class (`M`, `T`, `E`, `~M`, `~T`) |
| `technical_only_change_ratio` | `T` and `~T` / pairs with a known class |
| `unknown_classification_ratio` | `U` / all pairs; `coverage` is known (with estimates) / all pairs, `exact_coverage` the same without estimates |
| `validation_regression_count` | Pairs whose publish score fell or where a CRITICAL or HIGH rule appeared; pairs with a `FATAL` side or a format transition are not compared (`counts.compared`) |
| `unclassified_diff_ratio` | Unclassified report rows / all raw differences of the reported pairs |
| `source_availability_rate` | Publications still listed by the source / all publications |

Estimated pairs are counted apart (`counts.estimated_*`); unknown pairs are never guessed, and a
rate over known pairs is shown with its coverage. Changes of
identifiers (renumbering) are not counted yet; the report summary does not separate them.

## §14 Analyses derived in the page

Some analyses are computed by the page from data the reports already hold, so they need no new
reports and no storage:

- **Travel time** per line, direction and day type: for every trip of a stored timetable, from its
  first to its last time; the page shows the median on each side and, for matched trips, how many
  got longer, shorter or stayed the same. Only changed lines store timetables.
- **Share of trips by hour** over all lines: the report's trips per hour of first departure,
  summed and divided by all trips on each side; hours whose share moves by at least one point are
  listed.

Dwell times need arrival times at every stop, which reports do not store; they wait for a
change of the engine version (§11).

## §15 Field filling

The content signature pass (§4.2) also counts how often selected optional fields are filled and
stores it in `content.json` as `fields`: per file, its rows and, per field, the rows with a value.
A tracked column the file does not have counts as never filled. For `stops.txt` it also counts
stops outside Japan (latitude 20–46, longitude 122–154) and, among them, those that fit only with
latitude and longitude swapped (`coordinates`); the page warns about them, since their lines look
wrong on maps and in route comparisons. For `wheelchair_boarding`,
`wheelchair_accessible` and `bikes_allowed` only 1 and 2 count, since 0 means "no information".

| File | Fields |
|---|---|
| `stops.txt` | `wheelchair_boarding`, `platform_code`, `stop_code`, `stop_desc`, `parent_station` |
| `routes.txt` | `route_color`, `route_text_color`, `route_url`, `route_desc` |
| `trips.txt` | `wheelchair_accessible`, `bikes_allowed`, `trip_headsign`, `trip_short_name`, `shape_id` |
| `stop_times.txt` | `stop_headsign`, `pickup_type`, `drop_off_type`, `timepoint` |

New publications get the counts when they are analysed. Publications the page shows whose
record predates the counts are downloaded again and counted, at most 200 per run; there is no
separate pipeline. `export-web` writes the shares as `fields.json.gz`, and the page compares the
two selected publications. Whether a file such as `translations.txt` or `shapes.txt` exists is
already in the record's `files`.
