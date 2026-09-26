# gtfs-jp-monitor

Monitoring pipeline for GTFS feeds published on [gtfs-data.jp](https://gtfs-data.jp).

For every feed generation (identified by its permanent `gtfs_file_uid`), the pipeline
runs [GTFS Analyzer](https://github.com/ttezer/gtfs-analyzer), stores a compact,
language-neutral summary and produces generation-to-generation differences: validation
diffs and semantic change reports (stops, lines, routes, timetables, service days, fares).

Status: early development. Pipeline contracts are described in [docs/data-model.md](docs/data-model.md).

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
