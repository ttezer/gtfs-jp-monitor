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
feeds/<org_id>/<feed_id>/diffs/<analysis_key>/<old_uid>__<new_uid>.json
runs/<YYYY>/<run_id>.json
```

`analysis_key` is `<release_tag>__<gtfs_jp_profile>` (for example `v0.14.0__auto`). Results of
different releases or profiles live side by side and are never compared with each other.

### §4.1 Commit policy

- A run that changes no data writes no file, so it produces no commit.
- Run records are written only when data changed or a warning or failure occurred.
- Time-dependent values (run times, durations, observed `rid`) appear only in run records and
  the catalog, never in generation or diff files.

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

`.github/workflows/gtfs-jp-monitor.yml` runs daily at 04:00 JST and on demand: unit tests,
pinned analyzer installation, catalog sync, incremental analysis, and one commit to the data
repository when something changed. A single concurrency group guarantees one writer at a time.
