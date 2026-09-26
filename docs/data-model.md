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
(`export-web`, report bundles gzip-compressed) and a second job deploys it to GitHub Pages;
only that job has `pages: write` and `id-token: write`.

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
