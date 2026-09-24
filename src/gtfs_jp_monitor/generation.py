"""GTFS Analyzer JSON -> gtfs-jp-monitor-generation/1 (data-model §5).

A pure transformation: the same analyzer report and metadata always give the same record.
Human-readable text (titles, messages, structural error strings) is never copied.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass
from typing import Any

from .canonical import round1, yyyymmdd_to_iso
from .ids import require_path_id, require_uid

SCHEMA = "gtfs-jp-monitor-generation/1"
SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
CLASSES = ("SPEC", "INTEROP", "QUALITY", "ANALYTICS")
_RULE_ID_RE = re.compile(r"[A-Z]{2,4}_[0-9]{3}[a-z]?")
_FILE_NAME_RE = re.compile(r"[^/\\]+\.txt")
_SCORE_FIELDS = {
    "publish": "pub_score",
    "overall": "score",
    "spec": "spec_score",
    "interop": "interop_score",
    "quality": "quality_score",
    "analytics": "analytics_score",
}


class AnalyzerOutputError(ValueError):
    """The analyzer report does not have the expected shape."""


@dataclass(frozen=True)
class FeedMeta:
    org_id: str
    feed_id: str
    name: str


@dataclass(frozen=True)
class GenerationMeta:
    uid: str
    sha256: str
    published_at: str | None
    from_date: str
    to_date: str | None
    license: str


@dataclass(frozen=True)
class AnalyzerIdentity:
    version: str
    release_tag: str
    binary_sha256: str
    os: str
    arch: str
    gtfs_jp_profile: str


def fatal_code(raw: object) -> str:
    """Analyzer fatal codes are PascalCase (ZipUnreadable); records use UPPER_SNAKE (ZIP_UNREADABLE)."""
    if not isinstance(raw, str) or not raw:
        return "UNKNOWN_FATAL"
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", raw):
        return raw
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw).upper()
    return snake if re.fullmatch(r"[A-Z][A-Z0-9_]*", snake) else "UNKNOWN_FATAL"


def _date_or_none(value: object) -> str | None:
    if value in (None, 0, ""):
        return None
    try:
        return yyyymmdd_to_iso(value)  # type: ignore[arg-type]
    except ValueError as err:
        raise AnalyzerOutputError(f"invalid date {value!r}") from err


def _range(start: object, end: object) -> dict | None:
    s, e = _date_or_none(start), _date_or_none(end)
    if s is None and e is None:
        return None
    if s is None or e is None:
        raise AnalyzerOutputError(f"half-open range {start!r}..{end!r}")
    return {"start": s, "end": e}


def _bool(section: dict, key: str, where: str) -> bool:
    # A missing verdict must not silently become "not publishable".
    value = section.get(key)
    if not isinstance(value, bool):
        raise AnalyzerOutputError(f"{where}.{key} is not a boolean: {value!r}")
    return value


def _count(metrics: dict, key: str) -> int:
    value = metrics.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AnalyzerOutputError(f"metrics.{key} is not a non-negative integer: {value!r}")
    return value


def _base(feed: FeedMeta, gen: GenerationMeta, analyzer: AnalyzerIdentity) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "feed": {
            "org_id": require_path_id(feed.org_id, "org_id"),
            "feed_id": require_path_id(feed.feed_id, "feed_id"),
            "name": feed.name,
        },
        "generation": {
            "uid": require_uid(gen.uid),
            "sha256": gen.sha256,
            "published_at": gen.published_at,
            "from_date": gen.from_date,
            "to_date": gen.to_date,
            "license": gen.license,
        },
        "analyzer": {
            "version": analyzer.version,
            "release_tag": analyzer.release_tag,
            "binary_sha256": analyzer.binary_sha256,
            "platform": {"os": analyzer.os, "arch": analyzer.arch},
            "gtfs_jp_profile": analyzer.gtfs_jp_profile,
            # data-model §6.1: always from_date, never the run date.
            "validate_date": gen.from_date,
            "validate_date_source": "from_date",
        },
    }


def build_fatal(feed: FeedMeta, gen: GenerationMeta, analyzer: AnalyzerIdentity, code: str) -> dict[str, Any]:
    """Record for a generation that could not be analysed (analyzer fatal, timeout, download failure ...)."""
    doc = _base(feed, gen, analyzer)
    doc.update(
        validation_status="FATAL",
        fatal={"code": fatal_code(code)},
        partial=None,
        publishable=None,
        coverage_complete=None,
        is_gtfs_jp=None,
        scores=None,
        metrics=None,
        file_row_counts=None,
        summary=None,
        rules=None,
    )
    return doc


def build_generation(report: dict, feed: FeedMeta, gen: GenerationMeta, analyzer: AnalyzerIdentity) -> dict[str, Any]:
    """Transform one `gtfs-analyzer validate --json` report into a generation record."""
    if not isinstance(report, dict):
        raise AnalyzerOutputError("report is not an object")
    status = report.get("status")
    if status == "fatal":
        return build_fatal(feed, gen, analyzer, report.get("code"))  # type: ignore[arg-type]
    if status not in ("ok", "partial"):
        raise AnalyzerOutputError(f"unknown status {status!r}")

    validation_status = report.get("validation_status")
    if validation_status not in ("COMPLETE", "PARTIAL"):
        raise AnalyzerOutputError(f"unknown validation_status {validation_status!r}")
    if (status == "partial") != (validation_status == "PARTIAL"):
        raise AnalyzerOutputError(f"status {status!r} contradicts validation_status {validation_status!r}")

    reports = report.get("reports") or {}
    r1, r5 = reports.get("r1"), reports.get("r5")
    metrics = report.get("metrics")
    notices = report.get("notices")
    if not isinstance(r1, dict) or not isinstance(r5, dict) or not isinstance(metrics, dict) or not isinstance(notices, list):
        raise AnalyzerOutputError("report lacks reports.r1, reports.r5, metrics or notices")

    scores = {}
    for name, key in _SCORE_FIELDS.items():
        value = r5.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise AnalyzerOutputError(f"reports.r5.{key} missing")
        scores[name] = round1(value)

    rules: dict[str, dict] = {}
    by_severity = collections.Counter({s: 0 for s in SEVERITIES})
    by_class = collections.Counter({c: 0 for c in CLASSES})
    for notice in notices:
        rule_id, severity, rule_class = notice.get("rule_id"), notice.get("severity"), notice.get("rule_class")
        if not (isinstance(rule_id, str) and _RULE_ID_RE.fullmatch(rule_id)):
            raise AnalyzerOutputError(f"invalid rule_id {rule_id!r}")
        if severity not in SEVERITIES or rule_class not in CLASSES:
            raise AnalyzerOutputError(f"{rule_id}: unknown severity/class {severity!r}/{rule_class!r}")
        entry = rules.setdefault(rule_id, {"count": 0, "severity": severity, "class": rule_class})
        if (entry["severity"], entry["class"]) != (severity, rule_class):
            raise AnalyzerOutputError(f"{rule_id}: inconsistent severity/class across notices")
        entry["count"] += 1
        by_severity[severity] += 1
        by_class[rule_class] += 1

    file_row_counts: dict[str, int] = {}
    for stat in metrics.get("file_stats") or []:
        name, rows = stat.get("name"), stat.get("rows")
        if not (isinstance(name, str) and _FILE_NAME_RE.fullmatch(name)):
            raise AnalyzerOutputError(f"unexpected file name {name!r}")
        if not isinstance(rows, int) or isinstance(rows, bool) or rows < 0:
            raise AnalyzerOutputError(f"{name}: invalid row count {rows!r}")
        file_row_counts[name] = rows

    partial = None
    if validation_status == "PARTIAL":
        raw = report.get("partial") or {}

        def _names(key: str) -> list[str]:
            return sorted({str(v) for v in raw.get(key) or []})

        partial = {
            "root_structural_error_count": len(raw.get("root_structural_errors") or []),
            "unavailable_files": _names("unavailable_files"),
            "skipped_stages": _names("skipped_stages"),
            "skipped_checks": _names("skipped_checks"),
        }

    avg = metrics.get("avg_daily_trips")
    if not isinstance(avg, (int, float)) or isinstance(avg, bool):
        raise AnalyzerOutputError("metrics.avg_daily_trips missing")

    doc = _base(feed, gen, analyzer)
    doc.update(
        validation_status=validation_status,
        fatal=None,
        partial=partial,
        publishable=_bool(r1, "publishable", "reports.r1"),
        coverage_complete=_bool(r1, "coverage_complete", "reports.r1"),
        is_gtfs_jp=_bool(metrics, "is_gtfs_jp", "metrics"),
        scores=scores,
        metrics={
            "routes": _count(metrics, "route_count"),
            "stops": _count(metrics, "stop_count"),
            "trips": _count(metrics, "trip_count"),
            "shapes": _count(metrics, "shape_count"),
            "active_service_days": _count(metrics, "active_service_days"),
            "avg_daily_trips": round1(avg),
            "service_range": _range(metrics.get("service_start_date"), metrics.get("service_end_date")),
            "feed_validity": _range(metrics.get("feed_start_date"), metrics.get("feed_end_date")),
        },
        file_row_counts=file_row_counts,
        summary={"by_severity": dict(by_severity), "by_class": dict(by_class)},
        rules=rules,
    )
    return doc
