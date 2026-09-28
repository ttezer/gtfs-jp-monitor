# gtfs-jp-monitor

Monitoring pipeline for GTFS feeds published on [gtfs-data.jp](https://gtfs-data.jp).

**Web site: <https://ttezer.github.io/gtfs-jp-monitor/>** (updated daily; Turkish, English, Japanese)

For every feed generation (identified by its permanent `gtfs_file_uid`), the pipeline
runs [GTFS Analyzer](https://github.com/ttezer/gtfs-analyzer), stores a compact,
language-neutral summary and produces generation-to-generation differences: validation
diffs and semantic change reports (stops, lines, routes, timetables, service days, fares).

Status: running daily; development continues. Pipeline contracts are described in
[docs/data-model.md](docs/data-model.md).

The results are published daily at <https://ttezer.github.io/gtfs-jp-monitor/> (Turkish,
English, Japanese). A comparison can be shared by its link (data-model §10.1); each semantic
report is also published as JSON at `reports/<org_id>/<feed_id>/<old_uid>__<new_uid>.json.gz`
(data-model §10.0), and `status.json` tells when the site was last updated.

Each pair of publications is classified as a meaningful service change, technical only,
equivalent or unknown; pairs without a report are estimated from which files changed
(data-model §12). `metrics.json` gives per feed and in total the last 365
days: publication and meaningful-update frequency, change ratios, validation regressions and
coverage (§13); `fields.json.gz` gives how often selected optional fields such as wheelchair
information are filled and flags stops outside Japan or with swapped coordinates (§15).

## How it runs

A GitHub Actions workflow (`.github/workflows/gtfs-jp-monitor.yml`) runs every day at 04:10 JST
(GitHub may start it later): it syncs the catalog, analyses new publications, builds the semantic
reports, commits the results to a separate, private data repository and publishes the site to
GitHub Pages. A watchdog in the data repository checks every evening that the site was rebuilt
within 30 hours and re-enables the daily schedule if GitHub disabled it for inactivity
(data-model §10, §10.2).

## Running it yourself

Python 3.11 or newer; the pipeline needs nothing beyond the standard library. The commands work
on `--data-dir`, a checkout of a data repository (`install-analyzer` takes `--dest`); each has
`--help`:

| Command | Does |
|---|---|
| `sync-catalog` | Fetches the feed and publication catalog from gtfs-data.jp |
| `install-analyzer` | Downloads and verifies the pinned GTFS Analyzer release (`analyzer.lock.json`) |
| `analyze` | Analyses publications that have no record yet for the analyzer and profile |
| `semantic-reports` | Builds the missing change reports of the publications the page shows |
| `export-web` | Builds the site (`--html`) from the data directory |
| `semantic-report`, `rawdiff` | Compare two GTFS ZIP files directly |

```bash
PYTHONPATH=src python3 -m gtfs_jp_monitor sync-catalog --data-dir data
PYTHONPATH=src python3 -m gtfs_jp_monitor install-analyzer --dest analyzer
PYTHONPATH=src python3 -m gtfs_jp_monitor analyze --data-dir data --analyzer analyzer/gtfs-analyzer --limit 20
PYTHONPATH=src python3 -m gtfs_jp_monitor semantic-reports --data-dir data
PYTHONPATH=src python3 -m gtfs_jp_monitor export-web --data-dir data --release-tag v0.14.0 --html site/index.html
```

Results of a local analyzer build are only written into a directory marked as scratch
(data-model §6.2); published data comes from the workflow.

## Layout

| Path | Contents |
|---|---|
| `docs/` | Pipeline contracts (`data-model.md`) and semantic engine specifications |
| `schemas/` | JSON Schemas of the generated data |
| `src/gtfs_jp_monitor/` | Pipeline code |
| `src/gtfs_jp_semantic/` | Semantic change engine (`docs/semantic/`) |
| `web/prototype/` | The web page, filled by `export-web` |
| `tests/` | Unit tests |

Run the tests (schema tests need the `dev` extra, `jsonschema`):

```bash
PYTHONPATH=src python3 -m unittest discover -t . -s tests
```

Generated data is written to a separate data repository; raw GTFS ZIP files are never stored,
only a content signature of each (data-model §4.2).

The API client uses only the Python standard library and always verifies TLS certificates.
Python builds that ship without system CA certificates (for example the python.org macOS
installer) need a CA bundle, e.g. `SSL_CERT_FILE=$(python3 -m certifi)`.

## Data sources and licenses

Feed data comes from gtfs-data.jp and each transit operator. Every generation record keeps
the license published for its feed; follow that license when redistributing derived data.

The code of this repository is released under the [MIT License](LICENSE). It does not cover the
feed data or the data derived from it, which keep their own licenses.
