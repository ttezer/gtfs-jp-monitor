"""Command line entry point: `python -m gtfs_jp_monitor <command>`."""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

from .analyzer import PROFILES, AnalyzerBinary, Lock, install_release, pinned_release_for, sha256_file
from .catalog import sync_catalog
from .gtfsdatajp import BASE_URL, USER_AGENT, GtfsDataJpClient
from .ids import is_path_id
from .pipeline import run_analysis

DEFAULT_LOCK = Path(__file__).resolve().parents[2] / "analyzer.lock.json"


def _feed_key(text: str) -> tuple[str, str]:
    org_id, sep, feed_id = text.partition("/")
    if not sep or not (is_path_id(org_id) and is_path_id(feed_id)):
        raise argparse.ArgumentTypeError(f"expected ORG_ID/FEED_ID, got {text!r}")
    return org_id, feed_id


def _cmd_sync_catalog(args: argparse.Namespace) -> int:
    client = GtfsDataJpClient(base_url=args.base_url, min_interval=args.min_interval)

    def progress(index: int, total: int, key: tuple[str, str]) -> None:
        if args.progress and index % 25 == 0:
            print(f"[{index}/{total}] {key[0]}/{key[1]}", file=sys.stderr, flush=True)

    result = sync_catalog(client, Path(args.data_dir), only=set(args.only) if args.only else None, progress=progress)
    summary = {
        "feeds_scanned": result.feeds_scanned,
        "generations_seen": result.generations_seen,
        "new_generations": len(result.new_generations),
        "changed_files": result.changed_files,
        "warnings_by_code": dict(sorted(collections.Counter(w["code"] for w in result.warnings).items())),
        "requests": client.request_count,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.events_out:
        Path(args.events_out).write_text(json.dumps(result.events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.warnings_out:
        Path(args.warnings_out).write_text(
            json.dumps(result.warnings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


def _fetch_release_archive(url: str, dest: Path, max_bytes: int = 64 * 1024 * 1024) -> None:
    import urllib.request

    from .download import _HttpsOnlyRedirects

    opener = urllib.request.build_opener(_HttpsOnlyRedirects())
    with opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SystemExit(f"release archive larger than {max_bytes} bytes")
    dest.write_bytes(data)


def _cmd_install_analyzer(args: argparse.Namespace) -> int:
    lock = Lock.load(Path(args.lock))
    binary = install_release(lock, Path(args.dest), _fetch_release_archive)
    print(json.dumps({"binary": str(binary), "release_tag": lock.release_tag, "sha256": sha256_file(binary)}, indent=2))
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    lock = Lock.load(Path(args.lock))
    pinned = pinned_release_for(Path(args.analyzer), lock)
    binary = AnalyzerBinary.inspect(Path(args.analyzer), pinned_release=pinned)
    if binary.is_pinned and f"{binary.arch}-{binary.os}" != lock.production_platform and not args.allow_non_production_platform:
        raise SystemExit(
            f"pinned binary runs on {binary.arch}-{binary.os}; data-repository results must come from "
            f"{lock.production_platform} (data-model §6.2). Use --allow-non-production-platform only for scratch data."
        )
    extra = json.loads(Path(args.extra_warnings).read_text(encoding="utf-8")) if args.extra_warnings else None
    report = run_analysis(
        Path(args.data_dir), binary, args.profile, trigger=args.trigger, limit=args.limit,
        only=set(args.only) if args.only else None, timeout=args.timeout, extra_warnings=extra,
    )
    print(json.dumps({
        "run_id": report.run_id, "pinned": binary.is_pinned, "counts": report.counts,
        "changed_files": len(report.changed_files), "run_file": report.run_file,
        "warnings_by_code": dict(sorted(collections.Counter(w["code"] for w in report.warnings).items())),
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_rawdiff(args: argparse.Namespace) -> int:
    from gtfs_jp_semantic.rawdiff import diff_zips, gzip_bytes

    doc = diff_zips(Path(args.old), Path(args.new))
    Path(args.output).write_bytes(gzip_bytes(doc))
    print(json.dumps({
        "changes": len(doc["changes"]),
        "files": {name: meta["counts"] for name, meta in doc["files"].items() if meta["counts"]},
        "archive_warnings": len(doc["archive_warnings"]),
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_semantic_report(args: argparse.Namespace) -> int:
    from gtfs_jp_semantic.rawdiff import gzip_bytes
    from gtfs_jp_semantic.report import build_report

    from .canonical import write_json
    from .catalog import load_catalog
    from .store import diff_path

    root = Path(args.data_dir)
    org_id, feed_id = args.feed
    entries = load_catalog(root)[1].get((org_id, feed_id), {})
    pubs = []
    for uid in (args.old_uid, args.new_uid):
        if uid not in entries:
            raise SystemExit(f"{uid} is not a generation of {org_id}/{feed_id} in the catalog")
        pubs.append({k: entries[uid].get(k) for k in ("uid", "from_date", "to_date", "published_at", "memo")})
    quality, key = None, None
    if args.analysis_key:
        path = diff_path(root, org_id, feed_id, args.analysis_key, args.old_uid, args.new_uid)
        if path.exists():
            scores = json.loads(path.read_text(encoding="utf-8"))["scores"]
            if scores:
                quality, key = {"publish": scores["publish"], "overall": scores["overall"]}, args.analysis_key
    report, _ = build_report(Path(args.old), Path(args.new), feed={"org_id": org_id, "feed_id": feed_id},
                             old_pub=pubs[0], new_pub=pubs[1], summary_quality=quality, analysis_key=key)
    out = Path(args.output)
    if out.name.endswith(".gz"):
        out.write_bytes(gzip_bytes(report))
    else:
        write_json(out, report)
    print(json.dumps({"coverage": report["header"]["coverage"], "lines": report["summary"]["lines"],
                      "places": report["summary"]["places"]}, ensure_ascii=False, indent=2))
    return 0


def _cmd_semantic_reports(args: argparse.Namespace) -> int:
    from .changes import run_reports
    from .store import analysis_key

    key = analysis_key(Lock.load(Path(args.lock)).release_tag, args.profile)
    run = run_reports(Path(args.data_dir), key, limit=args.limit, only=set(args.only) if args.only else None,
                      max_seconds=args.max_minutes * 60 if args.max_minutes else None)
    print(json.dumps({"analysis_key": key, "counts": dict(sorted(run.counts.items())),
                      "failed": [i for i in run.items if i["action"] == "failed"][:50]}, ensure_ascii=False, indent=2))
    return 0


DEFAULT_TEMPLATE = Path(__file__).resolve().parents[2] / "web" / "prototype" / "index.html"


def _cmd_export_web(args: argparse.Namespace) -> int:
    from .webexport import build_export

    key = f"{args.release_tag}__{args.profile}"
    export, files = build_export(Path(args.data_dir), key, Path(args.analyzer) if args.analyzer else None)
    ext = ".json.gz" if args.bundle_format == "gz" else ".json"
    export["report_bundle_ext"] = ext
    if args.json:
        from .canonical import write_json
        write_json(Path(args.json), export)
    if args.html:
        # The report index is written next to the page and loaded after it, which keeps the page small.
        page = {k: v for k, v in export.items() if k not in ("report_index", "metrics")}
        payload = json.dumps(page, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        template = Path(args.template).read_text(encoding="utf-8")
        if template.count("/*__EXPORT__*/") != 1:
            raise SystemExit("template must contain exactly one /*__EXPORT__*/ placeholder")
        Path(args.html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.html).write_text(template.replace("/*__EXPORT__*/", payload), encoding="utf-8")
        from gtfs_jp_semantic.rawdiff import gzip_bytes

        from .canonical import dumps

        out = Path(args.html).parent / "reports"
        out.mkdir(parents=True, exist_ok=True)
        for old in [*out.rglob("*.json.gz"), *out.rglob("*.json")]:  # reports of an earlier export
            old.unlink()
        index = export["report_index"]
        (out / f"index{ext}").write_bytes(gzip_bytes(index) if ext == ".json.gz" else dumps(index).encode("utf-8"))
        for name, doc in files.items():  # reports/<org_id>/<feed_id>/<old_uid>__<new_uid><ext>
            path = out / f"{name}{ext}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(gzip_bytes(doc) if ext == ".json.gz" else dumps(doc).encode("utf-8"))
        (Path(args.html).parent / "metrics.json").write_text(dumps(export["metrics"]) + "\n", encoding="utf-8")
        # Operational status for monitoring (watchdog, storage check): the export's status plus the
        # size of the site just written.
        site = Path(args.html).parent
        status = dict(export["status"], site_bytes=sum(p.stat().st_size for p in site.rglob("*") if p.is_file()))
        (site / "status.json").write_text(json.dumps(status, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"feeds": len(export["feeds"]),
                      "generations": sum(len(f["generations"]) for f in export["feeds"]),
                      "reports": len(export["report_index"]), "report_files": len(files)}, indent=2))
    return 0


def _cmd_check_storage(args: argparse.Namespace) -> int:
    from .webexport import storage_warnings

    status = json.loads(Path(args.status).read_text(encoding="utf-8"))
    repo = args.repo_kb * 1024 if args.repo_kb is not None else None
    st = status["storage"]
    mib = lambda b: f"{b / 2**20:.1f} MiB" if b is not None else "unknown"
    lines = [f"| data repository (with history) | {mib(repo)} |", f"| data working tree | {mib(st['data_bytes'])} |",
             f"| reports ({st['reports']['count']}, avg {st['reports']['avg_bytes'] // 1024} KiB, "
             f"max {st['reports']['max_bytes'] // 1024} KiB) | {mib(st['reports']['bytes'])} |",
             f"| web site | {mib(status.get('site_bytes'))} |"]
    print("| Storage | Size |\n|---|---|\n" + "\n".join(lines))
    for w in storage_warnings(status, repo):
        print(f"::warning::{w}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gtfs_jp_monitor")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync-catalog", help="refresh catalog/feeds.json and catalog/generations.json")
    sync.add_argument("--data-dir", required=True, help="root of the data repository checkout")
    sync.add_argument("--only", type=_feed_key, action="append", metavar="ORG_ID/FEED_ID",
                      help="fetch only these feeds (repeatable); other catalog entries are kept")
    sync.add_argument("--base-url", default=BASE_URL)
    sync.add_argument("--min-interval", type=float, default=1.0, help="seconds between API requests (default 1.0)")
    sync.add_argument("--progress", action="store_true", help="print progress to stderr")
    sync.add_argument("--warnings-out", help="write the full warning list to this JSON file")
    sync.add_argument("--events-out", help="write one-off events (for the run record) to this JSON file")
    sync.set_defaults(func=_cmd_sync_catalog)

    install = sub.add_parser("install-analyzer", help="download, verify and install the pinned analyzer release")
    install.add_argument("--dest", required=True, help="directory for the gtfs-analyzer binary")
    install.add_argument("--lock", default=str(DEFAULT_LOCK))
    install.set_defaults(func=_cmd_install_analyzer)

    analyze = sub.add_parser("analyze", help="analyze generations that have no record for this analyzer and profile")
    analyze.add_argument("--data-dir", required=True)
    analyze.add_argument("--analyzer", required=True, help="path to the gtfs-analyzer binary")
    analyze.add_argument("--profile", default="auto", choices=PROFILES,
                         help="GTFS-JP profile, part of the analysis identity (default: auto)")
    analyze.add_argument("--lock", default=str(DEFAULT_LOCK))
    analyze.add_argument("--limit", type=int, help="analyze at most this many generations (newest first)")
    analyze.add_argument("--only", type=_feed_key, action="append", metavar="ORG_ID/FEED_ID")
    analyze.add_argument("--timeout", type=float, default=600, help="seconds per generation (default 600)")
    analyze.add_argument("--trigger", choices=("schedule", "workflow_dispatch", "local"), default="local")
    analyze.add_argument("--allow-non-production-platform", action="store_true")
    analyze.add_argument("--extra-warnings", help="JSON list of catalog events to include in the run record")
    analyze.set_defaults(func=_cmd_analyze)

    raw = sub.add_parser("rawdiff", help="list every difference between two GTFS ZIP files (semantic engine, part 1)")
    raw.add_argument("--old", required=True)
    raw.add_argument("--new", required=True)
    raw.add_argument("-o", "--output", required=True, help="output .json.gz (gtfs-jp-semantic-rawdiff/1)")
    raw.set_defaults(func=_cmd_rawdiff)

    rep = sub.add_parser("semantic-report", help="planner change report for two publications (semantic engine, part 3)")
    rep.add_argument("--data-dir", required=True, help="data repository checkout (catalog and validation diffs)")
    rep.add_argument("--feed", required=True, type=_feed_key, metavar="ORG_ID/FEED_ID")
    rep.add_argument("--old", required=True, help="old GTFS ZIP")
    rep.add_argument("--new", required=True, help="new GTFS ZIP")
    rep.add_argument("--old-uid", required=True)
    rep.add_argument("--new-uid", required=True)
    rep.add_argument("--analysis-key", help="e.g. v0.14.0__auto; links the validation diff when it exists")
    rep.add_argument("-o", "--output", required=True, help="output .report.json or .report.json.gz")
    rep.set_defaults(func=_cmd_semantic_report)

    reps = sub.add_parser("semantic-reports", help="build missing change reports of consecutive publications into the data repository")
    reps.add_argument("--data-dir", required=True)
    reps.add_argument("--profile", default="auto", choices=PROFILES)
    reps.add_argument("--lock", default=str(DEFAULT_LOCK), help="the analysis key uses the pinned release of this lock")
    reps.add_argument("--limit", type=int, help="build at most this many reports (newest pairs first)")
    reps.add_argument("--only", type=_feed_key, action="append", metavar="ORG_ID/FEED_ID")
    reps.add_argument("--max-minutes", type=float, help="time budget: start no new report after this many minutes")
    reps.set_defaults(func=_cmd_semantic_reports)

    web = sub.add_parser("export-web", help="export data for the web page (prototype)")
    web.add_argument("--data-dir", required=True)
    web.add_argument("--release-tag", required=True, help="analysis release, e.g. v0.14.0")
    web.add_argument("--profile", default="auto", choices=PROFILES)
    web.add_argument("--analyzer", help="gtfs-analyzer binary, used only to read rule titles (tr/en/ja)")
    web.add_argument("--json", help="write the export JSON here")
    web.add_argument("--html", help="write a self-contained page here")
    web.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    web.add_argument("--bundle-format", choices=("gz", "json"), default="gz",
                     help="report files next to the page: gzip (default) or plain JSON for hosts that do not serve .gz")
    web.set_defaults(func=_cmd_export_web)

    chk = sub.add_parser("check-storage", help="print storage sizes and warn past 75%% of a budget (data-model §10.2)")
    chk.add_argument("--status", required=True, help="status.json written by export-web")
    chk.add_argument("--repo-kb", type=int, help="size of the data repository with history, in KiB (GitHub API)")
    chk.set_defaults(func=_cmd_check_storage)

    args = parser.parse_args(argv)
    if getattr(args, "min_interval", 1.0) < 0.5:
        parser.error("--min-interval below 0.5 s is not allowed (be polite to gtfs-data.jp)")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
